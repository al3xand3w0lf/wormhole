"""Automatic rover -> nearest-base subscription.

Builds on the mutable `RoverRouter` (rover.py): once a rover's and a
trusted base's positions are both known, this module does the nearest-base
selection and calls `add_rover()`/`remove_rover()` for you.

WHAT THIS DOES AND DOES NOT DECIDE
-----------------------------------
A station's ROLE (base/rover/logger/stream) comes from its own IDENT frame
(streaming/frames.py, since the matching firmware change) - the server trusts
whatever a connecting station declares. But "which stations are trusted as a
*correction source*" is a different, security-relevant question: a station
merely claiming ROLE_BASE would, if trusted blindly, get to inject whatever it
sends as "corrections" pushed to every rover subscribed to it. So role=base
alone is not enough to create a router - the station id must ALSO be in
`config.STREAM_ROVER_AUTO_BASE_STATIONS` (or already a `STREAM_ROVER_BASES`
key, which is the same trust decision made by hand). Role only decides *when*
a trusted base's router is created (as soon as it identifies, rather than
requiring it to be present at process start) and *that* it is created at all
via role - never *whether* it is trusted to begin with. Wiring is in
server.py's `_ensure_base_router()`.

Rovers get no such gate: consuming corrections is not a trust boundary the
same way producing them is, and gating every rover by hand is exactly the
restart-requiring manual step this feature removes. See `_manual_rovers` below
for the one thing that IS excluded - stations already hand-pinned in
`STREAM_ROVER_BASES` or `STREAM_ROVER_STATIONS`, so a hand-configured pair is
never second-guessed by this module (per the design doc's "Backward
compatibility" section).

THE POLICY, IN ONE PLACE
-------------------------
- **Hold for a fix.** A rover with no 3D fix yet is a visible-but-unsubscribed
  candidate - no corrections on a guess. Exception: if exactly one base is
  known at all, there is no "nearest" decision to make, so it is subscribed
  immediately (see _evaluate()).
- **Nearest by ECEF baseline**, guarded by a max baseline
  (STREAM_ROVER_MAX_BASELINE_KM) beyond which a correction cannot help, and a
  switch margin (STREAM_ROVER_SWITCH_MARGIN_KM) so a rover sitting near the
  boundary between two bases does not flap between them every fix.
- **A role is matched by exact value, never by exclusion.** ROLE_ROVER_NTRIP
  (a rover that pulls its own corrections off an NTRIP caster and discards
  anything we push it) must never be subscribed, and neither must any role
  added on the device side after this was written. Testing `!= ROLE_BASE` or
  "anything rover-ish" would have auto-subscribed the NTRIP rover on the day
  its firmware shipped, occupied a slot, and had status() report a
  subscription that does nothing. It is listed in status() with that reason
  rather than omitted, because "absent" and "deliberately skipped" look the
  same to whoever is wondering where their rover went.
- **Disconnect clears state.** Position and subscription are dropped, not
  cached, on disconnect - a stale position is exactly what "hold" exists to
  avoid, and the device reconnects with a fresh IDENT in seconds regardless
  (matches the existing download/downloadfw reconnect pattern elsewhere in
  this codebase).
"""

import logging

from . import geo
from .frames import ROLE_ROVER, ROLE_ROVER_NTRIP
from .sinks import Sink

try:
    from pyubx2 import UBXReader
except ImportError:  # pragma: no cover - pyubx2 is a hard dependency in practice
    UBXReader = None

logger = logging.getLogger("streaming")

NAV_CLASS = 0x01
NAV_PVT_ID = 0x07
FIX_3D = 3


def _decode_arp(raw: bytes) -> tuple[float, float, float] | None:
    """A base station's own ARP (DF025/026/027), from its first 1005/1006.

    Same pyrtcm-first-then-getattr pattern as rover.py's
    _reference_station_id(), for the same reason: not every RTCM3 message
    carries these fields, and a message that does not is not an error.
    """
    try:
        from pyrtcm import RTCMReader

        parsed = RTCMReader.parse(raw)
        x = getattr(parsed, "DF025", None)
        y = getattr(parsed, "DF026", None)
        z = getattr(parsed, "DF027", None)
        if x is None or y is None or z is None:
            return None
        return (float(x), float(y), float(z))
    except Exception:  # noqa: BLE001 - not every RTCM3 message parses as one with an ARP
        return None


class BaseArpSink(Sink):
    """Publishes a base's RTCM3 into its RoverRouter (as RoverSourceSink does)
    and additionally decodes the base's own ARP, once, for RoverAutoDiscovery.

    A superset of RoverSourceSink rather than a wrapper around it: wrapping
    would mean every correction on the hot path pays for a second dispatch
    for the sake of one decode that only ever needs to succeed once per base.
    """

    def __init__(self, router, station_id: int, discovery: "RoverAutoDiscovery"):
        self.router = router
        self.station_id = station_id
        self.discovery = discovery
        self._arp_done = False

    def on_rtcm3(self, raw: bytes, stamp, sysclk: bool) -> None:
        self.router.publish(raw)
        if self._arp_done:
            return
        arp = _decode_arp(raw)
        if arp is not None:
            self._arp_done = True
            self.discovery.on_base_position(self.station_id, arp)


class RoverPositionSink(Sink):
    """Feeds a rover's own UBX-NAV-PVT fixes into RoverAutoDiscovery.

    Attached only to stations identifying with ROLE_ROVER (server.py). A
    station that never reaches a 3D fix simply never calls on_rover_fix() and
    stays an unsubscribed candidate - "hold for a fix" is enforced by this
    sink never firing, not by a check downstream.
    """

    def __init__(self, station_id: int, discovery: "RoverAutoDiscovery"):
        self.station_id = station_id
        self.discovery = discovery

    def on_ubx(self, raw: bytes, stamp, sysclk: bool) -> None:
        if UBXReader is None or len(raw) < 4 or raw[2] != NAV_CLASS or raw[3] != NAV_PVT_ID:
            return
        try:
            msg = UBXReader.parse(raw, parsebitfield=0)
        except Exception:  # noqa: BLE001 - a corrupt frame must never kill the loop
            return

        fix_type = getattr(msg, "fixType", 0)
        if fix_type < FIX_3D:
            return
        lat = getattr(msg, "lat", None)
        lon = getattr(msg, "lon", None)
        height = getattr(msg, "height", None)  # raw mm - see geo.from_navpvt()
        if lat is None or lon is None or height is None:
            return

        self.discovery.on_rover_fix(self.station_id, geo.from_navpvt(lat, lon, height), fix_type)


class RoverAutoDiscovery:
    """Subscribes a role=rover station to its nearest known base, live.

    `base_routers` is the SAME dict server.py keeps as `_base_routers` (passed
    by reference, never copied) - a base created by `_ensure_base_router()`
    becomes visible here with no extra wiring, and this class never creates or
    destroys a router itself, only calls add_rover()/remove_rover() on ones
    that already exist.
    """

    def __init__(self, base_routers: dict[int, "RoverRouter"], manual_rovers: set[int],
                 max_baseline_km: float, switch_margin_km: float):
        self.base_routers = base_routers
        self._manual_rovers = set(manual_rovers)
        self.max_baseline_m = max_baseline_km * 1000.0
        self.switch_margin_m = switch_margin_km * 1000.0

        self.roles: dict[int, int] = {}
        self.base_positions: dict[int, tuple] = {}
        self.rover_positions: dict[int, tuple] = {}
        self.rover_subscribed: dict[int, int] = {}

    # -- inputs ---------------------------------------------------------

    def on_ident(self, station_id: int, role: int) -> None:
        known = self.roles.get(station_id)
        self.roles[station_id] = role
        if role == ROLE_ROVER_NTRIP and known != role:
            # Said once per (re)connect, not per frame. Without it the only
            # trace of this decision is an absence, and an absence explains
            # nothing to someone asking why their rover is not subscribed.
            logger.info("rover auto-discovery: station %d declared rover_ntrip - "
                        "not a subscription candidate, it brings its own corrections",
                        station_id)
        if role == ROLE_ROVER and station_id not in self._manual_rovers:
            # A reconnect with an already-known position (rare - see the
            # module docstring on why disconnect clears it) can subscribe
            # immediately instead of waiting for a fresh NAV-PVT.
            self._evaluate(station_id)

    def on_base_position(self, base_id: int, ecef: tuple) -> None:
        if base_id in self.base_positions:
            return  # an ARP does not move; the first decode is definitive
        self.base_positions[base_id] = ecef
        logger.info("rover auto-discovery: base %d ARP decoded, re-evaluating candidates",
                    base_id)
        for rover_id, role in list(self.roles.items()):
            if role == ROLE_ROVER and rover_id not in self._manual_rovers:
                self._evaluate(rover_id)

    def on_rover_fix(self, station_id: int, ecef: tuple, fix_type: int) -> None:
        if station_id in self._manual_rovers:
            return
        self.rover_positions[station_id] = ecef
        self._evaluate(station_id)

    def on_disconnect(self, station_id: int) -> None:
        self._unsubscribe(station_id)
        self.rover_positions.pop(station_id, None)
        self.roles.pop(station_id, None)

    # -- policy -----------------------------------------------------------

    def _evaluate(self, rover_id: int) -> None:
        if self.roles.get(rover_id) != ROLE_ROVER or rover_id in self._manual_rovers:
            return

        candidates = {bid: pos for bid, pos in self.base_positions.items()
                      if bid in self.base_routers}
        if not candidates:
            return

        if rover_id not in self.rover_positions:
            # Hold for a fix, with one short-circuit: exactly one known base
            # (a router that has also produced a decodable ARP) means there is
            # nothing to choose between, so waiting buys nothing.
            if len(candidates) == 1:
                (only_base_id,) = candidates.keys()
                self._subscribe(rover_id, only_base_id)
            return

        rover_pos = self.rover_positions[rover_id]
        nearest_id = min(candidates, key=lambda bid: geo.ecef_distance(candidates[bid], rover_pos))
        nearest_dist = geo.ecef_distance(candidates[nearest_id], rover_pos)

        if nearest_dist > self.max_baseline_m:
            if rover_id in self.rover_subscribed:
                logger.info("rover auto-discovery: station %d out of range of every base "
                           "(nearest %.0fm > %.0fm) - unsubscribing",
                           rover_id, nearest_dist, self.max_baseline_m)
            self._unsubscribe(rover_id)
            return

        current = self.rover_subscribed.get(rover_id)
        if current == nearest_id:
            return
        if current is not None and current in candidates:
            current_dist = geo.ecef_distance(candidates[current], rover_pos)
            if current_dist - nearest_dist < self.switch_margin_m:
                return  # hysteresis: not a big enough win to switch

        self._subscribe(rover_id, nearest_id)

    def _subscribe(self, rover_id: int, base_id: int) -> None:
        current = self.rover_subscribed.get(rover_id)
        if current == base_id:
            return
        if current is not None:
            old_router = self.base_routers.get(current)
            if old_router is not None:
                old_router.remove_rover(rover_id)

        router = self.base_routers.get(base_id)
        if router is None:
            return
        router.add_rover(rover_id)
        self.rover_subscribed[rover_id] = base_id
        dist = self.base_positions.get(base_id)
        dist_m = geo.ecef_distance(dist, self.rover_positions[rover_id]) if (
            dist is not None and rover_id in self.rover_positions) else None
        logger.info("rover auto-discovery: station %d -> base %d%s",
                    rover_id, base_id, f" ({dist_m:.0f}m)" if dist_m is not None else " (only base known)")

    def _unsubscribe(self, rover_id: int) -> None:
        base_id = self.rover_subscribed.pop(rover_id, None)
        if base_id is None:
            return
        router = self.base_routers.get(base_id)
        if router is not None:
            router.remove_rover(rover_id)

    # -- observability ------------------------------------------------------

    def status(self) -> dict:
        """Per-rover candidate state: which base (if any), and why."""
        out = {}
        for rover_id, role in self.roles.items():
            if role == ROLE_ROVER_NTRIP:
                # Listed, not skipped. This station is a rover by every outward
                # sign, so leaving it out of the rover status makes a deliberate
                # policy look like a bug - and the honest answer would then exist
                # nowhere in the running system.
                out[str(rover_id)] = {
                    "subscribed_base": None,
                    "distance_m": None,
                    "has_fix": rover_id in self.rover_positions,
                    "not_a_candidate": "brings its own corrections (NTRIP)",
                }
                continue
            if role != ROLE_ROVER or rover_id in self._manual_rovers:
                continue
            base_id = self.rover_subscribed.get(rover_id)
            pos = self.rover_positions.get(rover_id)
            base_pos = self.base_positions.get(base_id) if base_id is not None else None
            dist = round(geo.ecef_distance(base_pos, pos), 1) if (pos and base_pos) else None
            out[str(rover_id)] = {
                "subscribed_base": base_id,
                "distance_m": dist,
                "has_fix": rover_id in self.rover_positions,
            }
        return out
