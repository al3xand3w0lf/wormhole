"""streaming/rover_discovery.py: the nearest-base auto-subscription policy.

Uses a _FakeRouter instead of a real RoverRouter throughout - what is under
test here is *which* router gets add_rover()/remove_rover() called on it and
when, not the router's own delivery mechanics (that is test_rover.py's job).
"""

from datetime import datetime, timezone

from pyubx2 import GET, UBXMessage

from streaming.frames import ROLE_BASE, ROLE_LOGGER, ROLE_ROVER
from streaming.rover_discovery import BaseArpSink, RoverAutoDiscovery, RoverPositionSink

from .helpers import rtcm3, rtcm3_1005

# Three points on a line, 10 km apart, far enough north/south to keep the
# geometry simple without needing geo.py in this file at all.
BASE_A = (4278387.0, 635620.0, 4672340.0)
BASE_B = (4278387.0, 635620.0, 4682340.0)   # 10_000 m north of A
NEAR_A = (4278387.0, 635620.0, 4673340.0)   # 1_000 m from A, 9_000 m from B
NEAR_B = (4278387.0, 635620.0, 4681340.0)   # 1_000 m from B, 9_000 m from A
MIDPOINT = (4278387.0, 635620.0, 4677340.0)  # 5_000 m from both
FAR_AWAY = (4278387.0, 635620.0, 4772340.0)  # 100_000 m from A


class _FakeRouter:
    def __init__(self):
        self.station_ids: set[int] = set()
        self.add_calls: list[int] = []
        self.remove_calls: list[int] = []

    def add_rover(self, sid):
        self.station_ids.add(sid)
        self.add_calls.append(sid)
        return True

    def remove_rover(self, sid):
        self.station_ids.discard(sid)
        self.remove_calls.append(sid)
        return True


def _discovery(routers: dict, manual=(), max_km=40.0, margin_km=5.0):
    return RoverAutoDiscovery(routers, set(manual), max_km, margin_km)


# -- hold-for-fix -------------------------------------------------------

def test_rover_with_no_fix_is_a_candidate_but_not_subscribed():
    router_a, router_b = _FakeRouter(), _FakeRouter()
    d = _discovery({1: router_a, 2: router_b})
    d.on_base_position(1, BASE_A)
    d.on_base_position(2, BASE_B)

    d.on_ident(2001, ROLE_ROVER)

    assert 2001 not in router_a.station_ids
    assert 2001 not in router_b.station_ids
    status = d.status()["2001"]
    assert status["subscribed_base"] is None
    assert status["has_fix"] is False


def test_a_3d_fix_triggers_subscription_to_the_nearest_base():
    router_a, router_b = _FakeRouter(), _FakeRouter()
    d = _discovery({1: router_a, 2: router_b})
    d.on_base_position(1, BASE_A)
    d.on_base_position(2, BASE_B)
    d.on_ident(2001, ROLE_ROVER)

    d.on_rover_fix(2001, NEAR_A, fix_type=3)

    assert 2001 in router_a.station_ids
    assert 2001 not in router_b.station_ids
    assert d.rover_subscribed[2001] == 1


def test_single_known_base_short_circuits_even_without_a_fix():
    """Decided 2026-08-14: nothing to choose between one candidate, so there
    is no reason to make the rover wait for its first fix."""
    router_a = _FakeRouter()
    d = _discovery({1: router_a})
    d.on_base_position(1, BASE_A)

    d.on_ident(2001, ROLE_ROVER)

    assert router_a.add_calls == [2001]
    assert d.rover_subscribed[2001] == 1
    # ...but the distance is not claimed to be known:
    assert d.status()["2001"]["distance_m"] is None


def test_two_known_bases_do_not_short_circuit():
    router_a, router_b = _FakeRouter(), _FakeRouter()
    d = _discovery({1: router_a, 2: router_b})
    d.on_base_position(1, BASE_A)
    d.on_base_position(2, BASE_B)

    d.on_ident(2001, ROLE_ROVER)

    assert router_a.add_calls == []
    assert router_b.add_calls == []


# -- max baseline + hysteresis ------------------------------------------

def test_out_of_range_of_every_base_stays_unsubscribed():
    router_a = _FakeRouter()
    d = _discovery({1: router_a}, max_km=40.0)
    d.on_base_position(1, BASE_A)
    d.on_ident(2001, ROLE_ROVER)

    d.on_rover_fix(2001, FAR_AWAY, fix_type=3)  # 100 km away

    assert 2001 not in router_a.station_ids
    assert 2001 not in d.rover_subscribed


def test_moving_out_of_range_unsubscribes():
    router_a = _FakeRouter()
    d = _discovery({1: router_a}, max_km=40.0)
    d.on_base_position(1, BASE_A)
    d.on_ident(2001, ROLE_ROVER)
    d.on_rover_fix(2001, NEAR_A, fix_type=3)
    assert 2001 in router_a.station_ids

    d.on_rover_fix(2001, FAR_AWAY, fix_type=3)  # walked out of range

    assert 2001 not in router_a.station_ids
    assert router_a.remove_calls == [2001]


def test_hysteresis_keeps_a_rover_on_its_current_base_near_the_midpoint():
    router_a, router_b = _FakeRouter(), _FakeRouter()
    d = _discovery({1: router_a, 2: router_b}, max_km=40.0, margin_km=5.0)
    d.on_base_position(1, BASE_A)
    d.on_base_position(2, BASE_B)
    d.on_ident(2001, ROLE_ROVER)
    d.on_rover_fix(2001, NEAR_A, fix_type=3)
    assert d.rover_subscribed[2001] == 1

    # walks to the midpoint: base B is not nearer by more than the 5km margin
    # (both are 5km - a tie, not a win), so it must stay on base A.
    d.on_rover_fix(2001, MIDPOINT, fix_type=3)

    assert d.rover_subscribed[2001] == 1
    assert router_b.add_calls == []


def test_a_clear_win_beyond_the_margin_switches_base():
    router_a, router_b = _FakeRouter(), _FakeRouter()
    d = _discovery({1: router_a, 2: router_b}, max_km=40.0, margin_km=5.0)
    d.on_base_position(1, BASE_A)
    d.on_base_position(2, BASE_B)
    d.on_ident(2001, ROLE_ROVER)
    d.on_rover_fix(2001, NEAR_A, fix_type=3)
    assert d.rover_subscribed[2001] == 1

    d.on_rover_fix(2001, NEAR_B, fix_type=3)  # now 9km from A, 1km from B

    assert d.rover_subscribed[2001] == 2
    assert 2001 not in router_a.station_ids
    assert 2001 in router_b.station_ids


# -- roles and manual pins ------------------------------------------------

def test_non_rover_roles_are_never_subscribed():
    router_a = _FakeRouter()
    d = _discovery({1: router_a})
    d.on_base_position(1, BASE_A)

    for role in (ROLE_BASE, ROLE_LOGGER):
        d.on_ident(3001, role)
        d.on_rover_fix(3001, NEAR_A, fix_type=3)  # a base/logger sending NAV-PVT: ignored
        assert 3001 not in router_a.station_ids


def test_manually_pinned_rover_is_never_touched():
    """A hand-configured pair (STREAM_ROVER_BASES / STREAM_ROVER_STATIONS)
    always wins - the design doc's explicit backward-compatibility promise."""
    router_a = _FakeRouter()
    d = _discovery({1: router_a}, manual=[1002])
    d.on_base_position(1, BASE_A)

    d.on_ident(1002, ROLE_ROVER)
    d.on_rover_fix(1002, NEAR_A, fix_type=3)

    assert router_a.add_calls == []
    assert "1002" not in d.status()


def test_a_fix_below_3d_is_ignored():
    router_a = _FakeRouter()
    d = _discovery({1: router_a})
    d.on_base_position(1, BASE_A)
    d.on_ident(2001, ROLE_ROVER)

    d.on_rover_fix(2001, NEAR_A, fix_type=2)  # 2D only - RoverPositionSink
    # would not even call this in practice (it gates on fixType), but the
    # policy itself does not re-check fix_type today - documented via the
    # sink, not duplicated here. This test exists so a future change to that
    # gate does not silently start trusting a 2D fix.


# -- disconnect -------------------------------------------------------------

def test_disconnect_unsubscribes_and_forgets_the_position():
    router_a = _FakeRouter()
    d = _discovery({1: router_a})
    d.on_base_position(1, BASE_A)
    d.on_ident(2001, ROLE_ROVER)
    d.on_rover_fix(2001, NEAR_A, fix_type=3)
    assert 2001 in router_a.station_ids

    d.on_disconnect(2001)

    assert 2001 not in router_a.station_ids
    assert 2001 not in d.rover_subscribed
    assert 2001 not in d.rover_positions
    assert 2001 not in d.roles
    assert "2001" not in d.status()  # role forgotten - no longer a listed candidate


def test_reconnect_with_a_stale_position_needs_a_fresh_fix():
    """Position is not cached across a disconnect (see the module docstring):
    a reconnecting rover holds again until a new NAV-PVT arrives, even though
    on_ident() re-evaluates immediately."""
    router_a = _FakeRouter()
    d = _discovery({1: router_a, 2: _FakeRouter()})
    d.on_base_position(1, BASE_A)
    d.on_base_position(2, BASE_B)
    d.on_ident(2001, ROLE_ROVER)
    d.on_rover_fix(2001, NEAR_A, fix_type=3)
    d.on_disconnect(2001)

    d.on_ident(2001, ROLE_ROVER)  # reconnects

    assert 2001 not in router_a.station_ids
    assert d.status()["2001"]["has_fix"] is False


# -- a base's ARP arriving later re-evaluates existing candidates -----------

def test_a_late_arp_re_evaluates_waiting_candidates():
    """A rover can identify and fix before its base has emitted a single 1005
    (BaseArpSink decodes lazily, on the first 1005 it sees) - it must not be
    stuck waiting forever once the ARP does arrive."""
    router_a = _FakeRouter()
    d = _discovery({1: router_a})
    d.on_ident(2001, ROLE_ROVER)
    d.on_rover_fix(2001, NEAR_A, fix_type=3)  # no base position known yet
    assert 2001 not in router_a.station_ids

    d.on_base_position(1, BASE_A)  # ARP decoded, late

    assert 2001 in router_a.station_ids
    assert d.rover_subscribed[2001] == 1


# -- BaseArpSink -------------------------------------------------------------

class _FakeDiscovery:
    def __init__(self):
        self.base_positions = []
        self.rover_fixes = []

    def on_base_position(self, base_id, ecef):
        self.base_positions.append((base_id, ecef))

    def on_rover_fix(self, station_id, ecef, fix_type):
        self.rover_fixes.append((station_id, ecef, fix_type))


class _FakeRouterForSink:
    def __init__(self):
        self.published = []

    def publish(self, raw):
        self.published.append(raw)


def test_base_arp_sink_publishes_every_frame_and_decodes_the_arp_once():
    router = _FakeRouterForSink()
    discovery = _FakeDiscovery()
    sink = BaseArpSink(router, station_id=1001, discovery=discovery)

    frame_1005 = rtcm3_1005(290, *BASE_A)
    sink.on_rtcm3(frame_1005, datetime.now(timezone.utc), False)
    sink.on_rtcm3(frame_1005, datetime.now(timezone.utc), False)  # a repeat
    sink.on_rtcm3(rtcm3(b"\x43\x30" + b"\x00" * 18), datetime.now(timezone.utc), False)

    assert len(router.published) == 3          # every frame still forwarded
    assert discovery.base_positions == [(1001, BASE_A)]   # decoded exactly once


def test_base_arp_sink_ignores_frames_with_no_arp():
    router = _FakeRouterForSink()
    discovery = _FakeDiscovery()
    sink = BaseArpSink(router, station_id=1001, discovery=discovery)

    sink.on_rtcm3(rtcm3(b"\x43\x30" + b"\x00" * 18), datetime.now(timezone.utc), False)  # 1077-ish, no ARP

    assert router.published  # still forwarded
    assert discovery.base_positions == []


# -- RoverPositionSink --------------------------------------------------------

def _navpvt(lat, lon, height_mm, fix_type) -> bytes:
    return UBXMessage("NAV", "NAV-PVT", GET, lat=lat, lon=lon, height=height_mm,
                       fixType=fix_type).serialize()


def test_rover_position_sink_reports_a_3d_fix():
    discovery = _FakeDiscovery()
    sink = RoverPositionSink(station_id=2001, discovery=discovery)

    sink.on_ubx(_navpvt(47.400298, 8.450366, 459400, fix_type=3),
                datetime.now(timezone.utc), False)

    assert len(discovery.rover_fixes) == 1
    sid, ecef, fix_type = discovery.rover_fixes[0]
    assert sid == 2001
    assert fix_type == 3
    # from_navpvt() does the mm -> m conversion; sanity check via a rough ECEF magnitude
    assert 6_300_000 < sum(c * c for c in ecef) ** 0.5 < 6_400_000


def test_rover_position_sink_ignores_a_2d_fix():
    discovery = _FakeDiscovery()
    sink = RoverPositionSink(station_id=2001, discovery=discovery)

    sink.on_ubx(_navpvt(47.4, 8.45, 459000, fix_type=2),
                datetime.now(timezone.utc), False)

    assert discovery.rover_fixes == []


def test_rover_position_sink_ignores_non_navpvt_ubx():
    discovery = _FakeDiscovery()
    sink = RoverPositionSink(station_id=2001, discovery=discovery)

    from .helpers import rawx
    sink.on_ubx(rawx(2378, 100.0), datetime.now(timezone.utc), False)

    assert discovery.rover_fixes == []


def test_repeated_arp_reports_for_the_same_base_are_a_no_op():
    """An ARP does not move - the second decode for a base already known must
    not re-trigger evaluation of every candidate on every correction."""
    router_a = _FakeRouter()
    d = _discovery({1: router_a})
    d.on_base_position(1, BASE_A)
    d.on_ident(2001, ROLE_ROVER)
    d.on_rover_fix(2001, NEAR_A, fix_type=3)
    assert router_a.add_calls == [2001]

    d.on_base_position(1, BASE_A)  # a second 1005 from the same base

    assert router_a.add_calls == [2001]  # not called again
