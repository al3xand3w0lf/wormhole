"""Admin endpoints of the GNSS maintenance tunnel (streaming/server.py).

test_gnss_tunnel.py covers the pipe itself. These cover the decisions the endpoints
make around it, because each one guards a failure that looks like a broken link
from the operator's chair:

  * a device that ANSWERED but REFUSED must not leave a listener behind - the
    tool used to print "tunnel open" for a session the firmware had declined
    (FW 1.72.1 refuses while a receiver recovery runs);
  * a device that did not answer at all must not either;
  * the listener must exist before the device is told to open its side;
  * the command sent to the device must be exactly what the firmware parses,
    including the rescue form.

The coroutines are called directly: the API-key dependency is FastAPI wiring,
not behaviour, and a TestClient would add an event-loop hop without testing
anything these endpoints decide.
"""

import asyncio

import pytest
from fastapi import HTTPException

from streaming import server
from streaming.gnss_tunnel import TunnelRegistry


class FakeWriter:
    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class FakeSession:
    """The parts of StationSession the endpoints touch."""

    def __init__(self, station_id=1020, connected=True, reply="starting bridge...\r\n",
                 raises=None):
        self.station_id = station_id
        self.connected = connected
        self.writer = FakeWriter() if connected else None
        self.write_lock = asyncio.Lock()
        self._reply = reply
        self._raises = raises
        self.commands = []
        self.listener_bound_when_sent = []

    async def send_cli(self, cmd, token, timeout):
        self.commands.append(cmd)
        # Record whether the tunnel's listener already existed at the moment the
        # device was told to open its side - the ordering the endpoint promises.
        self.listener_bound_when_sent.append(server.tunnel_registry.get(self.station_id) is not None)
        if self._raises is not None:
            raise self._raises
        return self._reply


class FakeRegistry:
    def __init__(self, *sessions):
        self._s = {s.station_id: s for s in sessions}

    def get(self, station_id):
        return self._s.get(station_id)


@pytest.fixture
def tunnels(monkeypatch):
    reg = TunnelRegistry()
    monkeypatch.setattr(server, "tunnel_registry", reg)
    return reg


def use_session(monkeypatch, session):
    monkeypatch.setattr(server, "registry", FakeRegistry(session))


# -- open ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_unknown_station_is_404(monkeypatch, tunnels):
    monkeypatch.setattr(server, "registry", FakeRegistry())
    with pytest.raises(HTTPException) as e:
        await server.gnsstunnel_open(4242, server.GnssTunnelRequest(), "k")
    assert e.value.status_code == 404
    assert tunnels.get(4242) is None


@pytest.mark.asyncio
async def test_open_disconnected_station_is_409_and_binds_nothing(monkeypatch, tunnels):
    use_session(monkeypatch, FakeSession(connected=False))
    with pytest.raises(HTTPException) as e:
        await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")
    assert e.value.status_code == 409
    assert tunnels.get(1020) is None


@pytest.mark.asyncio
async def test_open_accepted_binds_loopback_before_the_device_is_told(monkeypatch, tunnels):
    session = FakeSession()
    use_session(monkeypatch, session)
    try:
        res = await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")
        assert res["ok"] is True
        assert session.commands == ["gnssbridge auto"]
        # The listener existed when the command went out.
        assert session.listener_bound_when_sent == [True]
        assert res["tunnel"]["host"] == "127.0.0.1"
        assert res["tunnel"]["port"] > 0
        assert tunnels.get(1020) is not None
    finally:
        await tunnels.close(1020)


@pytest.mark.asyncio
async def test_open_refused_by_device_is_409_and_closes_the_listener(monkeypatch, tunnels):
    """The case that shipped wrong: the device answered, but with a refusal.

    FW 1.72.1 declines a session while a receiver recovery is running. Before this
    check the listener stayed bound and the tool reported an open tunnel - a port
    into a session that does not exist, indistinguishable from a broken link.
    """
    refusal = ("bridge NOT started: a receiver recovery is pending or running"
               " - retry in ~30 s\r\n")
    use_session(monkeypatch, FakeSession(reply=refusal))
    with pytest.raises(HTTPException) as e:
        await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")
    assert e.value.status_code == 409
    # The operator must see WHY, not just that it failed.
    assert "receiver recovery" in e.value.detail
    assert tunnels.get(1020) is None


@pytest.mark.asyncio
async def test_open_device_silent_is_502_and_closes_the_listener(monkeypatch, tunnels):
    use_session(monkeypatch, FakeSession(raises=asyncio.TimeoutError()))
    with pytest.raises(HTTPException) as e:
        await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")
    assert e.value.status_code == 502
    assert tunnels.get(1020) is None


@pytest.mark.asyncio
async def test_open_twice_is_409_and_keeps_the_first_tunnel(monkeypatch, tunnels):
    session = FakeSession()
    use_session(monkeypatch, session)
    try:
        first = await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")
        with pytest.raises(HTTPException) as e:
            await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")
        assert e.value.status_code == 409
        # The first session is untouched and the device was not told a second time.
        assert tunnels.get(1020).port == first["tunnel"]["port"]
        assert session.commands == ["gnssbridge auto"]
    finally:
        await tunnels.close(1020)


@pytest.mark.asyncio
async def test_open_passes_pinned_baud_and_idle(monkeypatch, tunnels):
    session = FakeSession()
    use_session(monkeypatch, session)
    try:
        req = server.GnssTunnelRequest(baud="921600", idle_s=1800)
        await server.gnsstunnel_open(1020, req, "k")
        assert session.commands == ["gnssbridge 921600 1800"]
    finally:
        await tunnels.close(1020)


@pytest.mark.asyncio
async def test_open_rescue_builds_the_firmware_rescue_syntax(monkeypatch, tunnels):
    """`gnssbridge rescue <baud> <switch> [idle]` - the order the firmware parses.
    A swapped pair would park the boot ROM at 230400 and wait for a training
    sequence it can never lock onto."""
    session = FakeSession(reply="rescue at 9600 baud (will follow the tool up)\r\n")
    use_session(monkeypatch, session)
    try:
        req = server.GnssTunnelRequest(baud="9600", rescue=True, switch_baud=230400, idle_s=1800)
        await server.gnsstunnel_open(1020, req, "k")
        assert session.commands == ["gnssbridge rescue 9600 230400 1800"]
    finally:
        await tunnels.close(1020)


@pytest.mark.asyncio
async def test_open_rescue_with_auto_falls_back_to_9600(monkeypatch, tunnels):
    """"auto" means the LIVE link rate - meaningless for a receiver stuck in its
    boot ROM, which answers at 9600. The endpoint must not send "auto" there."""
    session = FakeSession(reply="rescue at 9600 baud\r\n")
    use_session(monkeypatch, session)
    try:
        req = server.GnssTunnelRequest(rescue=True)
        await server.gnsstunnel_open(1020, req, "k")
        assert session.commands == ["gnssbridge rescue 9600 0"]
    finally:
        await tunnels.close(1020)


def test_request_rejects_a_baud_that_is_not_a_number():
    # Validation happens before anything is bound or sent.
    with pytest.raises(Exception):
        server.GnssTunnelRequest(baud="fast")


# -- switch -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_switch_without_a_tunnel_is_409(monkeypatch, tunnels):
    """A switch with no session open is certainly a mistake - the rate would
    apply to nothing, and the operator would keep waiting for a flash that is
    not running."""
    use_session(monkeypatch, FakeSession())
    with pytest.raises(HTTPException) as e:
        await server.gnsstunnel_switch(1020, server.GnssTunnelSwitchRequest(), "k")
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_switch_without_a_rate_lets_the_device_use_the_opened_one(monkeypatch, tunnels):
    """The operator named the rate when opening the tunnel. Making them repeat it
    while a flash tool is counting is how a typo gets typed."""
    session = FakeSession(reply="switch requested - the link moves within a second\r\n")
    use_session(monkeypatch, session)
    try:
        await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")
        session.commands.clear()
        res = await server.gnsstunnel_switch(1020, server.GnssTunnelSwitchRequest(), "k")
        assert session.commands == ["gnssbridge switch"]
        assert res["ok"] is True
    finally:
        await tunnels.close(1020)


@pytest.mark.asyncio
async def test_switch_passes_an_explicit_rate(monkeypatch, tunnels):
    session = FakeSession(reply="switch requested - the link moves within a second\r\n")
    use_session(monkeypatch, session)
    try:
        await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")
        session.commands.clear()
        await server.gnsstunnel_switch(
            1020, server.GnssTunnelSwitchRequest(baud=230400), "k")
        assert session.commands == ["gnssbridge switch 230400"]
    finally:
        await tunnels.close(1020)


@pytest.mark.asyncio
async def test_switch_refused_by_device_is_409(monkeypatch, tunnels):
    """"Command arrived" is not "command executed" - the same distinction that
    made `open` report a tunnel for a session the device had declined."""
    refusal = ("switch REFUSED: no rate given and none configured, or outside"
               " 4800..921600\r\n")
    session = FakeSession(reply=refusal)
    use_session(monkeypatch, session)
    try:
        await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")
        with pytest.raises(HTTPException) as e:
            await server.gnsstunnel_switch(1020, server.GnssTunnelSwitchRequest(), "k")
        assert e.value.status_code == 409
        assert "REFUSED" in e.value.detail
    finally:
        await tunnels.close(1020)


@pytest.mark.asyncio
async def test_switch_rejects_a_rate_outside_the_firmware_bounds():
    """The device refuses these too, but a round trip over LTE to learn it is a
    round trip during a flash."""
    import pydantic
    for bad in (1200, 1_000_000):
        with pytest.raises(pydantic.ValidationError):
            server.GnssTunnelSwitchRequest(baud=bad)


# -- close / status -------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_stops_the_device_then_the_listener(monkeypatch, tunnels):
    session = FakeSession()
    use_session(monkeypatch, session)
    await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")

    session._reply = "stop requested - the session ends within a second\r\n"
    res = await server.gnsstunnel_close(1020, "k")
    assert session.commands[-1] == "gnssbridge stop"
    # The device was told while the listener still existed.
    assert session.listener_bound_when_sent[-1] is True
    assert res["listener_closed"] is True
    assert res["stats"]["station_id"] == 1020
    assert tunnels.get(1020) is None


@pytest.mark.asyncio
async def test_close_still_tears_down_when_the_device_does_not_answer(monkeypatch, tunnels):
    """The device has its own idle timeout and session cap precisely so a lost
    operator cannot strand it; our side must not stay bound waiting for it."""
    session = FakeSession()
    use_session(monkeypatch, session)
    await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")

    session._raises = asyncio.TimeoutError()
    res = await server.gnsstunnel_close(1020, "k")
    assert res["listener_closed"] is True
    assert "no answer" in res["device_response"]
    assert tunnels.get(1020) is None


@pytest.mark.asyncio
async def test_close_without_a_tunnel_is_not_an_error(monkeypatch, tunnels):
    use_session(monkeypatch, FakeSession(reply="no bridge session is running\r\n"))
    res = await server.gnsstunnel_close(1020, "k")
    assert res["listener_closed"] is False
    assert res["stats"] is None


@pytest.mark.asyncio
async def test_status_lists_open_tunnels(monkeypatch, tunnels):
    use_session(monkeypatch, FakeSession())
    try:
        assert (await server.gnsstunnel_status("k")) == {"tunnels": []}
        await server.gnsstunnel_open(1020, server.GnssTunnelRequest(), "k")
        listed = (await server.gnsstunnel_status("k"))["tunnels"]
        assert [t["station_id"] for t in listed] == [1020]
    finally:
        await tunnels.close(1020)
