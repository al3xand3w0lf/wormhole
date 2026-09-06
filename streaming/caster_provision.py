"""Automatic mountpoint provisioning on the bundled NTRIP caster.

A station that identifies with `role=base` (frames.ROLE_BASE, out of its IDENT
frame) gets a mountpoint on the caster in `caster/` without anyone editing
`.env` and without restarting either process: a password is generated,
`source.auth` and `sourcetable.dat` are rewritten, the caster is SIGHUPed, and
an NtripCasterSink is attached to the station's live session. Reconfiguring a
device to be a base is then the whole procedure.

WHAT IS AND IS NOT GATED HERE
------------------------------
The role byte from IDENT is the *only* condition. That is a deliberate
difference from `rover_discovery.py`, which additionally requires a base to be
named in `STREAM_ROVER_AUTO_BASE_STATIONS` before it may act as a correction
source, and the reason the two are not one decision:

- A base trusted by `rover_discovery` gets its RTCM3 *pushed into rovers in
  this fleet*, which are receivers we own and which will compute a position
  from whatever arrives. Trusting a bare wire claim there means a station can
  make our rovers believe anything.
- A mountpoint here only makes that station's own RTCM3 *available to whoever
  asks for it, under that station's own name*, next to the archive it already
  gets. A client picks it deliberately. Nothing downstream is steered by it.

The stream socket has no TLS and no per-station authentication beyond
`STREAM_CLI_SECRET`, so "identified as role=base" is a claim, not proof - which
is exactly why the two gates are separate and only the weaker consequence runs
off the claim alone. Turn the whole mechanism off with
`STREAM_CASTER_AUTO_ENABLE=false` (the default) if that trade is wrong for a
given deployment.

STATE LIVES IN .env, NOT HERE
------------------------------
Every provisioning writes `STREAM_CASTER_STATIONS` and
`STREAM_CASTER_PASSWORDS` back into `.env`, the same two keys
`caster/generate_config.py` owns. That is what makes the two idempotent with
respect to each other: a `caster/setup.sh` re-run after an auto-provisioning
keeps the generated password instead of issuing a new one and orphaning the
mountpoint, and a restart of this server re-reads what it provisioned last time
instead of rotating every base's credential.
"""

import logging
import secrets

from dotenv import set_key

from . import caster_config
from .rover_discovery import decode_arp
from .sinks import Sink

logger = logging.getLogger("streaming")


class CasterAutoProvision:
    """Owns the bundled caster's station set, live.

    Seeded from what `.env` and the existing sourcetable already say, so the
    first rewrite is a no-op for everything that was provisioned by hand or by
    `caster/setup.sh`.
    """

    def __init__(self, env_file, stations: set[int], passwords: dict[int, str],
                 etc_dir=caster_config.ETC_DIR):
        self.env_file = env_file
        self.etc_dir = etc_dir
        self.passwords = dict(passwords)
        # A station with a password but missing from STREAM_CASTER_STATIONS is
        # still a mountpoint - it is in source.auth. Union, so a rewrite never
        # revokes one that only one of the two keys knew about.
        self.stations = set(stations) | set(passwords)
        self.positions = caster_config.read_positions(etc_dir)
        self.auto_provisioned: set[int] = set()

    # -- inputs -----------------------------------------------------------

    def password_for(self, station_id: int) -> str | None:
        return self.passwords.get(station_id)

    def on_base_ident(self, station_id: int) -> str | None:
        """Provision `station_id` if it has no mountpoint yet; return its password.

        Idempotent: a station that already has one costs a dict lookup and
        writes nothing, which is the common case on every reconnect.
        """
        existing = self.passwords.get(station_id)
        if existing is not None and station_id in self.stations:
            return existing

        password = existing or secrets.token_urlsafe(18)
        self.passwords[station_id] = password
        self.stations.add(station_id)
        if not self._apply():
            # Never hand back a credential the caster was not told about - the
            # push would just fail the handshake every 5s forever.
            if existing is None:
                self.passwords.pop(station_id, None)
            self.stations.discard(station_id)
            return None
        self.auto_provisioned.add(station_id)
        logger.info("caster: auto-provisioned mountpoint %d (role=base, no config edit)",
                    station_id)
        return password

    def on_base_position(self, station_id: int, ecef: tuple) -> None:
        """A base's decoded ARP - the position Millipede's NEAR lookup needs.

        Only ever called once per station per process (CasterArpSink stops
        after its first successful decode), so this rewrite is not on any hot
        path.
        """
        if station_id not in self.stations:
            return
        lat, lon = caster_config.ecef_to_geodetic(*ecef)
        if self.positions.get(station_id) == (lat, lon):
            return
        self.positions[station_id] = (lat, lon)
        if self._apply(write_env=False):
            logger.info("caster: station %d position %.5f/%.5f in the sourcetable "
                        "(NEAR routing)", station_id, lat, lon)

    # -- output -----------------------------------------------------------

    def _mounts(self) -> list:
        return [
            caster_config.Mountpoint(
                station_id=station,
                password=self.passwords[station],
                lat=self.positions.get(station, (None, None))[0],
                lon=self.positions.get(station, (None, None))[1],
            )
            for station in sorted(self.stations)
            if station in self.passwords
        ]

    def _apply(self, write_env: bool = True) -> bool:
        """Write both caster files, reload the caster, persist to .env.

        Returns whether the *files* were written - a caster that could not be
        reloaded (not running yet, wrong uid) still leaves correct config on
        disk for its next start, so that is a warning inside reload_caster(),
        not a failure here.
        """
        try:
            caster_config.write_config(self._mounts(), self.etc_dir)
        except OSError:
            logger.exception("caster: cannot write %s - mountpoint not provisioned",
                             self.etc_dir)
            return False
        caster_config.reload_caster(self.etc_dir)
        if write_env:
            self._write_env()
        return True

    def _write_env(self) -> None:
        """Persist to the same two keys caster/generate_config.py owns.

        Best-effort: a read-only .env costs the mountpoint its persistence
        across a restart, not this session's push, so it must not undo what
        already works on the caster side.
        """
        try:
            set_key(str(self.env_file), "STREAM_CASTER_STATIONS",
                    ",".join(str(s) for s in sorted(self.stations)), quote_mode="never")
            set_key(str(self.env_file), "STREAM_CASTER_PASSWORDS",
                    ",".join(f"{s}:{self.passwords[s]}" for s in sorted(self.stations)
                             if s in self.passwords), quote_mode="never")
            set_key(str(self.env_file), "STREAM_CASTER_ENABLE", "true", quote_mode="never")
        except Exception:  # noqa: BLE001 - see the docstring
            logger.exception("caster: cannot persist provisioning to %s - the mountpoint "
                             "works now but will not survive a restart", self.env_file)

    # -- observability ------------------------------------------------------

    def status(self) -> dict:
        return {
            "enabled": True,
            "etc_dir": str(self.etc_dir),
            "caster_pid": caster_config.find_caster_pid(self.etc_dir),
            "mountpoints": {
                str(station): {
                    "auto_provisioned": station in self.auto_provisioned,
                    "position": list(self.positions[station])
                    if station in self.positions else None,
                }
                for station in sorted(self.stations) if station in self.passwords
            },
        }


class CasterArpSink(Sink):
    """Decodes a base's own ARP once, for the caster sourcetable.

    Separate from rover_discovery.BaseArpSink rather than folded into it: that
    one exists only where a base also has a RoverRouter, and a base can perfectly
    well have a mountpoint and no rovers in this fleet at all. The decode stops
    after the first hit, so the duplicate on a base that has both is one extra
    parse attempt per RTCM3 frame for the first second of its connection.
    """

    def __init__(self, station_id: int, provision: CasterAutoProvision):
        self.station_id = station_id
        self.provision = provision
        self._arp_done = station_id in provision.positions

    def on_rtcm3(self, raw: bytes, stamp, sysclk: bool) -> None:
        if self._arp_done:
            return
        arp = decode_arp(raw)
        if arp is not None:
            self._arp_done = True
            self.provision.on_base_position(self.station_id, arp)
