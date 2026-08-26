"""NtripCasterSink: SOURCE handshake, forwarding, and caster-outage resilience."""

import asyncio

import pytest

from streaming import ntrip
from streaming.ntrip import NtripCasterSink, _QUEUE_MAXSIZE


async def _close_and_wait(sink: NtripCasterSink) -> None:
    """close() only cancels - without awaiting the task, its socket teardown can
    still be running (and, worse, its reconnect loop still scheduled) after the
    test function returns, bleeding into whatever test runs next. Every test that
    calls on_rtcm3() must tear down through this, not sink.close() alone."""
    sink.close()
    if sink._task is not None:
        try:
            await sink._task
        except asyncio.CancelledError:
            pass


class FakeCaster:
    """A minimal NTRIP 1.0 caster: reads the SOURCE handshake, answers, then
    records whatever bytes follow."""

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.handshakes: list[bytes] = []
        self.received = bytearray()
        self._data_event = asyncio.Event()
        self._server: asyncio.base_events.Server | None = None
        self.host = "127.0.0.1"
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        while True:
            hdr = await reader.readline()
            if hdr in (b"\r\n", b"\n", b""):
                break
        self.handshakes.append(line)

        if not self.accept:
            writer.write(b"ERROR - Bad Password\r\n")
            await writer.drain()
            writer.close()
            return

        writer.write(b"ICY 200 OK\r\n")
        await writer.drain()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                self.received.extend(chunk)
                self._data_event.set()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass

    async def wait_for_bytes(self, n: int, timeout: float = 2.0) -> None:
        async def _wait():
            while len(self.received) < n:
                self._data_event.clear()
                if len(self.received) >= n:
                    return
                await self._data_event.wait()

        await asyncio.wait_for(_wait(), timeout)

    async def close(self) -> None:
        self._server.close()
        await self._server.wait_closed()


@pytest.mark.asyncio
async def test_forwards_rtcm3_after_handshake():
    caster = FakeCaster()
    await caster.start()
    try:
        sink = NtripCasterSink(caster.host, caster.port, "1001", "secret")
        sink.on_rtcm3(b"\xd3\x00\x03ABC", None, False)
        await caster.wait_for_bytes(6)
        await _close_and_wait(sink)

        assert caster.handshakes == [b"SOURCE secret 1001\r\n"]
        assert bytes(caster.received) == b"\xd3\x00\x03ABC"
    finally:
        await caster.close()


@pytest.mark.asyncio
async def test_frames_stay_in_order():
    caster = FakeCaster()
    await caster.start()
    try:
        sink = NtripCasterSink(caster.host, caster.port, "1001", "secret")
        sink.on_rtcm3(b"AAA", None, False)
        sink.on_rtcm3(b"BBB", None, False)
        sink.on_rtcm3(b"CCC", None, False)
        await caster.wait_for_bytes(9)
        await _close_and_wait(sink)

        assert bytes(caster.received) == b"AAABBBCCC"
    finally:
        await caster.close()


@pytest.mark.asyncio
async def test_rejected_handshake_does_not_raise():
    """A wrong password must be logged, not crash the frame-routing caller."""
    caster = FakeCaster(accept=False)
    await caster.start()
    try:
        sink = NtripCasterSink(caster.host, caster.port, "1001", "wrong")
        sink.on_rtcm3(b"AAA", None, False)
        await asyncio.sleep(0.2)

        assert caster.handshakes == [b"SOURCE wrong 1001\r\n"]
        await _close_and_wait(sink)
    finally:
        await caster.close()


@pytest.mark.asyncio
async def test_unreachable_caster_does_not_raise(monkeypatch):
    # A host that never answers (dropped, not refused, as some sandboxed networks
    # do) must not hang the reconnect loop - _CONNECT_TIMEOUT bounds it. Use a
    # non-routable address (TEST-NET-1, RFC 5737) to force exactly that.
    monkeypatch.setattr(ntrip, "_CONNECT_TIMEOUT", 0.3)
    sink = NtripCasterSink("192.0.2.1", 2102, "1001", "secret")
    sink.on_rtcm3(b"AAA", None, False)
    await asyncio.sleep(0.5)
    await _close_and_wait(sink)


@pytest.mark.asyncio
async def test_queue_full_drops_instead_of_blocking(monkeypatch):
    # No caster reachable at all - the queue fills up and must never block or
    # raise out of on_rtcm3.
    monkeypatch.setattr(ntrip, "_CONNECT_TIMEOUT", 0.3)
    sink = NtripCasterSink("192.0.2.1", 2102, "1001", "secret")
    for _ in range(_QUEUE_MAXSIZE + 100):
        sink.on_rtcm3(b"X", None, False)
    await _close_and_wait(sink)


def test_close_without_any_frames_is_safe():
    sink = NtripCasterSink("127.0.0.1", 2102, "1001", "secret")
    sink.close()


def test_station_passwords_parses_id_colon_password_pairs():
    from streaming.config import _station_passwords

    assert _station_passwords("1001:pw1,1002:pw2") == {1001: "pw1", 1002: "pw2"}
    assert _station_passwords(" 1001 : pw1 ; 1002:pw2 ") == {1001: "pw1", 1002: "pw2"}
    # Malformed entries (no password, non-numeric station) are dropped, not raised.
    assert _station_passwords("1001:,notanumber:pw,1002:pw2") == {1002: "pw2"}
    assert _station_passwords("") == {}


def test_make_sinks_attaches_caster_sink_only_for_configured_stations(monkeypatch):
    from streaming import config, server

    monkeypatch.setattr(config, "STREAM_CASTER_ENABLE", True)
    monkeypatch.setattr(config, "STREAM_CASTER_PASSWORDS", {1001: "secret"})
    monkeypatch.setattr(config, "STREAM_ROVER_AUTO_ENABLE", False)

    sinks_1001 = server._make_sinks(1001)
    sinks_1002 = server._make_sinks(1002)

    assert any(isinstance(s, NtripCasterSink) for s in sinks_1001)
    assert not any(isinstance(s, NtripCasterSink) for s in sinks_1002)


def test_caster_targets_read_per_target_keys(monkeypatch):
    from streaming.config import _caster_targets

    monkeypatch.setenv("STREAM_CASTER_PARTNER_HOST", "ntrip.example.org")
    monkeypatch.setenv("STREAM_CASTER_PARTNER_PORT", "2102")
    monkeypatch.setenv("STREAM_CASTER_PARTNER_PASSWORDS", "1001:pw1")
    # A second target with nothing but a password list falls back to the
    # bundled caster's defaults (loopback, 2101).
    monkeypatch.setenv("STREAM_CASTER_BKG_PASSWORDS", "1001:shared,1002:shared")

    partner, bkg = _caster_targets("partner, bkg")

    assert (partner.name, partner.host, partner.port) == ("partner", "ntrip.example.org", 2102)
    assert partner.passwords == {1001: "pw1"}
    assert (bkg.host, bkg.port) == ("127.0.0.1", 2101)
    # A caster with one global source password repeats it per station.
    assert bkg.passwords == {1001: "shared", 1002: "shared"}


def test_caster_target_name_maps_to_key_prefix(monkeypatch):
    """The name only builds the key prefix: upper-cased, "-" becomes "_"."""
    from streaming.config import _caster_targets

    monkeypatch.setenv("STREAM_CASTER_MILLIPEDE_AWARE_PORT", "2103")
    target, = _caster_targets("millipede-aware")
    assert target.name == "millipede-aware"
    assert target.port == 2103


def test_caster_target_can_be_disabled_without_removing_its_config(monkeypatch):
    from streaming.config import _caster_targets

    monkeypatch.setenv("STREAM_CASTER_KEPT_PASSWORDS", "1001:pw")
    monkeypatch.setenv("STREAM_CASTER_OFF_PASSWORDS", "1001:pw")
    monkeypatch.setenv("STREAM_CASTER_OFF_ENABLE", "false")

    assert [t.name for t in _caster_targets("kept,off")] == ["kept"]
    assert _caster_targets("") == ()


def test_make_sinks_attaches_one_sink_per_configured_target(monkeypatch):
    from streaming import config, server

    monkeypatch.setattr(config, "STREAM_CASTER_ENABLE", False)
    monkeypatch.setattr(config, "STREAM_ROVER_AUTO_ENABLE", False)
    monkeypatch.setattr(config, "STREAM_CASTER_TARGETS", (
        config.CasterTarget("a", "127.0.0.1", 2102, {1001: "pw-a"}),
        config.CasterTarget("b", "127.0.0.1", 2104, {1001: "pw-b", 1002: "pw-b"}),
    ))

    casters_1001 = [s for s in server._make_sinks(1001) if isinstance(s, NtripCasterSink)]
    casters_1002 = [s for s in server._make_sinks(1002) if isinstance(s, NtripCasterSink)]

    # The same station goes to every target that has a mountpoint for it...
    assert sorted(s._port for s in casters_1001) == [2102, 2104]
    # ... and a station only provisioned on one caster reaches only that one.
    assert [s._port for s in casters_1002] == [2104]


def test_make_sinks_combines_the_bundled_caster_with_extra_targets(monkeypatch):
    from streaming import config, server

    monkeypatch.setattr(config, "STREAM_CASTER_ENABLE", True)
    monkeypatch.setattr(config, "STREAM_CASTER_PORT", 2101)
    monkeypatch.setattr(config, "STREAM_CASTER_PASSWORDS", {1001: "bundled"})
    monkeypatch.setattr(config, "STREAM_ROVER_AUTO_ENABLE", False)
    monkeypatch.setattr(config, "STREAM_CASTER_TARGETS", (
        config.CasterTarget("extra", "127.0.0.1", 2104, {1001: "pw"}),
    ))

    casters = [s for s in server._make_sinks(1001) if isinstance(s, NtripCasterSink)]
    assert sorted(s._port for s in casters) == [2101, 2104]
