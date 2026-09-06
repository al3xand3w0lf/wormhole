"""Automatic caster mountpoints off a role=base IDENT.

The invariant most of these guard is not "a file was written" but that the two
writers of sourcetable.dat/source.auth - caster/generate_config.py at setup time
and CasterAutoProvision at runtime - can never take a station away from each
other. That is what makes "reconfigure a device to be a base" the whole
procedure.
"""

from datetime import datetime, timezone

import pytest

from streaming import caster_config
from streaming.caster_config import Mountpoint
from streaming.caster_provision import CasterArpSink, CasterAutoProvision

from .helpers import rtcm3, rtcm3_1005

# ETH Hoenggerberg-ish, in ECEF metres.
BASE_ECEF = (4325000.0, 648000.0, 4670000.0)


@pytest.fixture
def etc(tmp_path, monkeypatch):
    """An etc/ dir with no caster behind it - reload_caster() finds no pid and
    warns, which is exactly the "caster not running yet" path and must not stop
    anything from being written."""
    d = tmp_path / "etc"
    d.mkdir()
    (d / "caster.yaml").write_text("listen:\n  - port: 2101\n")
    return d


def _provision(etc, tmp_path, stations=(), passwords=None):
    env = tmp_path / ".env"
    env.write_text("API_KEY=x\nSTREAM_CASTER_STATIONS=\nSTREAM_CASTER_PASSWORDS=\n")
    return CasterAutoProvision(env, set(stations), dict(passwords or {}), etc_dir=etc)


# ---------------------------------------------------------------- rendering


def test_sourcetable_marks_real_stations_as_non_virtual():
    """Field 12 is Millipede's virtual-base flag: 1 on a real station is the
    "Mount Point Taken" trap (caster/README.md), so it is worth a test of its
    own rather than being implied by a golden string."""
    line = caster_config.render_sourcetable([Mountpoint(1001, "pw", 47.4, 8.5)]).splitlines()[0]
    fields = line.split(";")
    assert fields[1] == "1001" and fields[12] == "0"


def test_near_entry_only_appears_once_a_station_has_a_real_position():
    without = caster_config.render_sourcetable([Mountpoint(1001, "pw")])
    with_pos = caster_config.render_sourcetable([Mountpoint(1001, "pw", 47.4, 8.5)])
    assert "NEAR" not in without
    assert "0.00000;0.00000" in without  # the placeholder, not a location
    assert "NEAR" in with_pos


def test_positions_survive_a_write_read_round_trip(etc):
    caster_config.write_config([Mountpoint(1001, "pw", 47.40716, 8.51061),
                                Mountpoint(1002, "pw2")], etc)
    # 1002 has only the placeholder and must not come back as a position at
    # 0/0 - the Gulf of Guinea is not where an unprovisioned station is.
    assert caster_config.read_positions(etc) == {1001: (47.40716, 8.51061)}


def test_source_auth_is_not_world_readable(etc):
    caster_config.write_config([Mountpoint(1001, "secret")], etc)
    assert (etc / "source.auth").stat().st_mode & 0o077 == 0
    assert "secret" in (etc / "source.auth").read_text()


# ------------------------------------------------------------- provisioning


def test_a_base_gets_a_mountpoint_and_it_lands_in_both_files(etc, tmp_path):
    p = _provision(etc, tmp_path)
    password = p.on_base_ident(2001)

    assert password
    assert f"2001:wormhole:{password}" in (etc / "source.auth").read_text()
    assert "STR;2001;station-2001;" in (etc / "sourcetable.dat").read_text()


def test_provisioning_is_idempotent_across_reconnects(etc, tmp_path):
    p = _provision(etc, tmp_path)
    first = p.on_base_ident(2001)
    before = (etc / "source.auth").read_text()

    assert p.on_base_ident(2001) == first
    # Not merely "same password": a rewrite on every reconnect would SIGHUP the
    # caster on every reconnect too.
    assert (etc / "source.auth").read_text() == before


def test_a_new_base_never_disturbs_an_existing_mountpoint(etc, tmp_path):
    p = _provision(etc, tmp_path, stations=(1001,), passwords={1001: "hand-configured"})
    p.on_base_ident(2001)

    auth = (etc / "source.auth").read_text()
    assert "1001:wormhole:hand-configured" in auth
    assert "2001:wormhole:" in auth


def test_a_station_configured_by_hand_is_returned_not_reissued(etc, tmp_path):
    p = _provision(etc, tmp_path, stations=(1001,), passwords={1001: "hand-configured"})
    assert p.on_base_ident(1001) == "hand-configured"
    # Rotating a credential someone else issued would take the mountpoint down
    # on every caster that has the old one, this one included.
    assert not (etc / "source.auth").exists() or "hand-configured" in (etc / "source.auth").read_text()


def test_a_password_is_not_handed_out_if_the_files_cannot_be_written(etc, tmp_path, monkeypatch):
    p = _provision(etc, tmp_path)

    def boom(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(caster_config, "write_config", boom)
    assert p.on_base_ident(2001) is None
    # The caster was never told about this station, so a sink pushing with a
    # password would just fail its handshake every 5s forever.
    assert p.password_for(2001) is None
    assert 2001 not in p.stations


def test_provisioning_persists_to_env_for_the_next_start(etc, tmp_path):
    p = _provision(etc, tmp_path)
    password = p.on_base_ident(2001)

    env = (tmp_path / ".env").read_text()
    assert "STREAM_CASTER_STATIONS=2001" in env
    assert f"STREAM_CASTER_PASSWORDS=2001:{password}" in env
    # Without this a restart would leave the sink off until someone re-ran
    # setup.sh, which is the manual step being removed.
    assert "STREAM_CASTER_ENABLE=true" in env


def test_env_is_written_so_a_setup_rerun_keeps_what_was_provisioned(etc, tmp_path):
    """generate_config.py reads exactly these two keys and regenerates both
    files from them - an auto-provisioned station missing from either would be
    silently dropped from the caster by the next caster/setup.sh run."""
    p = _provision(etc, tmp_path, stations=(1001,), passwords={1001: "hand"})
    p.on_base_ident(2001)

    env = (tmp_path / ".env").read_text()
    assert "STREAM_CASTER_STATIONS=1001,2001" in env
    assert "1001:hand" in env and "2001:" in env


# ------------------------------------------------------------------ position


def test_the_arp_fills_in_the_position_and_enables_near(etc, tmp_path):
    p = _provision(etc, tmp_path)
    p.on_base_ident(2001)
    assert "NEAR" not in (etc / "sourcetable.dat").read_text()

    sink = CasterArpSink(2001, p)
    sink.on_rtcm3(rtcm3_1005(2001, *BASE_ECEF), datetime.now(timezone.utc), False)

    table = (etc / "sourcetable.dat").read_text()
    assert "0.00000;0.00000;0" not in table  # the station's placeholder is gone
    assert "NEAR" in table


def test_the_arp_sink_stops_after_the_first_decode(etc, tmp_path):
    p = _provision(etc, tmp_path)
    p.on_base_ident(2001)
    sink = CasterArpSink(2001, p)
    frame = rtcm3_1005(2001, *BASE_ECEF)
    sink.on_rtcm3(frame, datetime.now(timezone.utc), False)
    before = (etc / "sourcetable.dat").read_text()

    sink.on_rtcm3(frame, datetime.now(timezone.utc), False)
    # An ARP does not move, and a rewrite per 1005 would be a rewrite per second.
    assert (etc / "sourcetable.dat").read_text() == before


def test_an_rtcm3_message_without_an_arp_is_not_an_error(etc, tmp_path):
    p = _provision(etc, tmp_path)
    p.on_base_ident(2001)
    sink = CasterArpSink(2001, p)
    sink.on_rtcm3(rtcm3(b"\x43\x30" + b"\x00" * 18), datetime.now(timezone.utc), False)
    assert 2001 not in p.positions


def test_a_restart_keeps_positions_already_in_the_sourcetable(etc, tmp_path):
    """Otherwise the first rewrite after a restart resets every station to the
    placeholder - and takes the NEAR entry down with it - until each base's ARP
    happens to come round again."""
    caster_config.write_config([Mountpoint(1001, "pw", 47.40716, 8.51061)], etc)
    p = _provision(etc, tmp_path, stations=(1001,), passwords={1001: "pw"})
    p.on_base_ident(2001)

    table = (etc / "sourcetable.dat").read_text()
    assert "47.40716;8.51061" in table
    assert "NEAR" in table


# ------------------------------------------------------------------ wiring


def test_a_base_identifying_gets_a_live_sink_without_a_restart(etc, tmp_path, monkeypatch):
    """The whole feature in one assertion: _make_sinks() has already run for
    this station (it connected as something else), and the caster sink still
    has to appear on the existing session."""
    from streaming import config, server
    from streaming.ntrip import NtripCasterSink

    monkeypatch.setattr(config, "STREAM_CASTER_ENABLE", False)
    monkeypatch.setattr(config, "STREAM_CASTER_PASSWORDS", {})
    monkeypatch.setattr(config, "STREAM_CASTER_TARGETS", ())
    monkeypatch.setattr(config, "STREAM_ROVER_AUTO_ENABLE", False)
    monkeypatch.setattr(server, "_caster_provision", _provision(etc, tmp_path))

    session = server.registry.get_or_create(2001)
    try:
        assert not [s for s in session.sinks if isinstance(s, NtripCasterSink)]
        server._provision_caster_mountpoint(2001)

        casters = [s for s in session.sinks if isinstance(s, NtripCasterSink)]
        assert [s.mountpoint for s in casters] == ["2001"]
        assert any(isinstance(s, CasterArpSink) for s in session.sinks)
    finally:
        session.close()
        server.registry._sessions.pop(2001, None)


def test_the_live_sink_is_attached_once_not_per_reconnect(etc, tmp_path, monkeypatch):
    from streaming import config, server
    from streaming.ntrip import NtripCasterSink

    monkeypatch.setattr(config, "STREAM_CASTER_ENABLE", False)
    monkeypatch.setattr(config, "STREAM_CASTER_PASSWORDS", {})
    monkeypatch.setattr(config, "STREAM_CASTER_TARGETS", ())
    monkeypatch.setattr(config, "STREAM_ROVER_AUTO_ENABLE", False)
    monkeypatch.setattr(server, "_caster_provision", _provision(etc, tmp_path))

    session = server.registry.get_or_create(2001)
    try:
        server._provision_caster_mountpoint(2001)
        server._provision_caster_mountpoint(2001)
        assert len([s for s in session.sinks if isinstance(s, NtripCasterSink)]) == 1
    finally:
        session.close()
        server.registry._sessions.pop(2001, None)


def test_a_new_station_gets_its_sink_from_make_sinks(etc, tmp_path, monkeypatch):
    """The first-connect path: no session exists yet, so the password has to be
    ready by the time _make_sinks() runs."""
    from streaming import config, server
    from streaming.ntrip import NtripCasterSink

    monkeypatch.setattr(config, "STREAM_CASTER_ENABLE", False)
    monkeypatch.setattr(config, "STREAM_CASTER_PASSWORDS", {})
    monkeypatch.setattr(config, "STREAM_CASTER_TARGETS", ())
    monkeypatch.setattr(config, "STREAM_ROVER_AUTO_ENABLE", False)
    monkeypatch.setattr(server, "_caster_provision", _provision(etc, tmp_path))
    monkeypatch.setitem(server._station_roles, 2001, 1)  # ROLE_BASE

    server._provision_caster_mountpoint(2001)
    sinks = server._make_sinks(2001)

    assert [s.mountpoint for s in sinks if isinstance(s, NtripCasterSink)] == ["2001"]
    assert any(isinstance(s, CasterArpSink) for s in sinks)


def test_nothing_is_provisioned_while_the_feature_is_off(etc, tmp_path, monkeypatch):
    from streaming import server

    monkeypatch.setattr(server, "_caster_provision", None)
    server._provision_caster_mountpoint(2001)  # must not raise
    assert not (etc / "source.auth").exists()


# ------------------------------------------------------------- reload target


def test_the_caster_is_found_when_it_was_started_with_a_relative_path(etc):
    """Found live: the systemd unit passes an absolute config path, but a
    caster started by hand from the repo carries a relative one. Matching the
    resolved path as a string then finds nothing, and every reload is silently
    skipped - the mountpoint is on disk and the caster never learns about it."""
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "-c", "./caster.yaml"],
        cwd=etc,
    )
    try:
        assert caster_config.find_caster_pid(etc) == proc.pid
        # Another caster on the same host (several do run here) must not match.
        assert caster_config.find_caster_pid(etc.parent) is None
    finally:
        proc.kill()
        proc.wait()


def test_a_missing_caster_is_a_warning_not_an_exception(tmp_path):
    assert caster_config.find_caster_pid(tmp_path) is None
    assert caster_config.reload_caster(tmp_path) is False
