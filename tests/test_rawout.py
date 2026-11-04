"""Raw stream out over NTRIP (streaming/rawout.py).

Covers what the raw stream out promises: the receiver's UBX and
RTCM3 in arrival order and nothing private, both NTRIP versions, accounts
that only see their own stations, a slow client cut off without touching the
others, and the refusals to start.
"""

import asyncio
import base64
import shutil
import ssl
import subprocess

import pytest
import pytest_asyncio

from streaming import config, rawout, server
from streaming.frames import ID_SENSOR_INA219
from streaming.rawout import Account, RawOutHub, RawOutServer, RawOutSink
from streaming.sinks import FileSink
from streaming.station import StationRegistry

from .helpers import heartbeat, ident, rawx, rtcm3, sensor, ubx

WEEK = 2378
TOW = 3 * 86400 + 14 * 3600 + 12.0

ACCOUNTS = {
    "proc": Account("proc", "s3cret", None),
    "alice": Account("alice", "pw", frozenset({1001})),
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _auth(user, pw):
    return "Authorization: Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode() + "\r\n"


async def _request(port, mount, user=None, pw=None, v2=False, ssl_ctx=None):
    """Send one NTRIP GET; return (reader, writer, response head as bytes)."""
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=ssl_ctx, server_hostname="localhost" if ssl_ctx else None)
    req = f"GET /{mount} HTTP/1.{'1' if v2 else '0'}\r\nUser-Agent: NTRIP test\r\n"
    if v2:
        req += "Ntrip-Version: Ntrip/2.0\r\nHost: localhost\r\n"
    if user is not None:
        req += _auth(user, pw)
    writer.write((req + "\r\n").encode())
    await writer.drain()
    first = await asyncio.wait_for(reader.readline(), 5)
    head = first
    if not first.startswith(b"ICY"):
        while True:
            line = await asyncio.wait_for(reader.readline(), 5)
            head += line
            if line in (b"\r\n", b""):
                break
    return reader, writer, head


async def _read_exact(reader, n):
    return await asyncio.wait_for(reader.readexactly(n), 5)


async def _read_chunk(reader):
    size = int((await asyncio.wait_for(reader.readline(), 5)).strip(), 16)
    data = await _read_exact(reader, size)
    assert await _read_exact(reader, 2) == b"\r\n"
    return data


@pytest_asyncio.fixture
async def ntrip():
    hub = RawOutHub(dict(ACCOUNTS), buffer_limit=1 << 20, max_clients=4)
    srv = RawOutServer(hub, "127.0.0.1", 0)
    await srv.start()
    yield hub, srv
    await srv.close()


async def _wait_clients(hub, n):
    for _ in range(100):
        if hub.client_count() == n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {n} clients, have {hub.client_count()}")


# --------------------------------------------------------------------------
# accounts and the refusals to start
# --------------------------------------------------------------------------
def test_parse_users():
    accounts, errors = rawout.parse_users("proc:a:b:c:*, alice:pw:1001+1020 ,")
    assert errors == []
    assert accounts["proc"] == Account("proc", "a:b:c", None)
    assert accounts["alice"].stations == frozenset({1001, 1020})
    assert accounts["alice"].may_read(1020) and not accounts["alice"].may_read(1027)


@pytest.mark.parametrize("text", ["proc:pw", "proc:pw:10x1", ":pw:*", "proc::*",
                                  "proc:a:*,proc:b:*"])
def test_parse_users_rejects(text):
    _, errors = rawout.parse_users(text)
    assert errors


def test_config_error(tmp_path):
    ok = {"proc": ACCOUNTS["proc"]}
    assert rawout.config_error(False, {}, [], True, None, None) is None
    assert "empty" in rawout.config_error(True, {}, [], False, None, None)
    assert "STREAM_RAWOUT_USERS" in rawout.config_error(True, ok, ["bad"], False, None, None)
    assert "certificate" in rawout.config_error(True, ok, [], True, tmp_path / "c.pem", None)
    assert rawout.config_error(True, ok, [], False, None, None) is None


# --------------------------------------------------------------------------
# the whole path: device -> route_frame -> RawOutSink -> NTRIP client
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_device_to_client_ubx_and_rtcm3_only(tmp_path, monkeypatch, ntrip):
    hub, srv = ntrip
    monkeypatch.setattr(config, "STREAM_DIR", tmp_path)
    monkeypatch.setattr(server, "registry", StationRegistry(
        lambda sid: [FileSink(tmp_path, sid), RawOutSink(hub, sid, str(sid))]))
    data = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    data_port = data.sockets[0].getsockname()[1]

    _, dev = await asyncio.open_connection("127.0.0.1", data_port)
    dev.write(ident(1001) + rawx(WEEK, TOW))
    await dev.drain()
    for _ in range(100):
        if hub.knows(1001):
            break
        await asyncio.sleep(0.01)

    reader, client, head = await _request(srv.port, "1001", "proc", "s3cret")
    assert head == b"ICY 200 OK\r\n"
    await _wait_clients(hub, 1)

    nav = ubx(0x01, 0x07, bytes(92))                     # NAV-PVT, any content
    corr = rtcm3(b"\x3e\xd0" + b"\x42" * 20)
    dev.write(nav + sensor(ID_SENSOR_INA219, "<Iiii", 1, 2, 3, 4) + heartbeat()
              + corr + rawx(WEEK, TOW + 1))
    await dev.drain()

    expected = nav + corr + rawx(WEEK, TOW + 1)
    assert await _read_exact(reader, len(expected)) == expected
    # Nothing private followed: the next bytes are the next receiver frame.
    dev.write(nav)
    await dev.drain()
    assert await _read_exact(reader, len(nav)) == nav

    st = hub.status()["stations"]["1001"]
    assert st["clients"] == 1 and st["bytes_out"] == len(expected) + len(nav)

    client.close()
    dev.close()
    data.close()
    await data.wait_closed()
    await _wait_clients(hub, 0)
    server.registry.close_all()


def test_make_sinks_attaches_rawout(monkeypatch, tmp_path):
    hub = RawOutHub({}, 1024, 1)
    monkeypatch.setattr(server, "_rawout_hub", hub)
    monkeypatch.setattr(server, "_caster_provision", None)
    monkeypatch.setattr(server, "_discovery", None)
    monkeypatch.setattr(server, "_base_routers", {})
    monkeypatch.setattr(config, "STREAM_CASTER_ENABLE", False)
    monkeypatch.setattr(config, "STREAM_CASTER_TARGETS", ())
    monkeypatch.setattr(config, "STREAM_DIR", tmp_path)
    monkeypatch.setitem(server._station_names, 4711, "Wolf Lab")
    sinks = server._make_sinks(4711)
    assert any(isinstance(s, RawOutSink) for s in sinks)
    assert hub.knows(4711)
    assert hub.status()["stations"]["4711"]["label"] == "Wolf_Lab_4711"
    for s in sinks:
        s.close()


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ntrip2_is_chunked(ntrip):
    hub, srv = ntrip
    RawOutSink(hub, 1001, "A_1001")
    reader, writer, head = await _request(srv.port, "1001", "proc", "s3cret", v2=True)
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Transfer-Encoding: chunked" in head and b"gnss/data" in head
    await _wait_clients(hub, 1)
    frames = [rawx(WEEK, TOW), rtcm3(b"\x3e\xd0" + b"\x01" * 5)]
    for f in frames:
        hub.publish(1001, f)
    assert [await _read_chunk(reader), await _read_chunk(reader)] == frames
    writer.close()


@pytest.mark.asyncio
async def test_sourcetable_lists_only_the_accounts_stations(ntrip):
    hub, srv = ntrip
    for sid in (1001, 1020):
        RawOutSink(hub, sid, f"S_{sid}")
    reader, _, head = await _request(srv.port, "", "alice", "pw")
    assert head.startswith(b"SOURCETABLE 200 OK")
    body = await asyncio.wait_for(reader.read(), 5)
    assert b"STR;1001;S_1001;" in body and b"1020" not in body
    assert body.endswith(b"ENDSOURCETABLE\r\n")

    reader, _, head = await _request(srv.port, "", "proc", "s3cret", v2=True)
    assert head.startswith(b"HTTP/1.1 200 OK") and b"gnss/sourcetable" in head
    body = await asyncio.wait_for(reader.read(), 5)
    assert b"STR;1001;" in body and b"STR;1020;" in body


@pytest.mark.asyncio
async def test_station_not_allowed_looks_like_unknown(ntrip):
    hub, srv = ntrip
    RawOutSink(hub, 1020, "1020")
    _, _, head = await _request(srv.port, "1020", "alice", "pw", v2=True)
    assert head.startswith(b"HTTP/1.1 404")
    _, _, head = await _request(srv.port, "9999", "alice", "pw", v2=True)
    assert head.startswith(b"HTTP/1.1 404")
    _, _, head = await _request(srv.port, "1020", "alice", "pw")
    assert head.startswith(b"SOURCETABLE 200 OK")
    assert hub.client_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("user,pw", [(None, None), ("proc", "wrong"), ("nobody", "s3cret")])
async def test_bad_credentials(ntrip, user, pw):
    hub, srv = ntrip
    RawOutSink(hub, 1001, "1001")
    for v2 in (False, True):
        _, _, head = await _request(srv.port, "1001", user, pw, v2=v2)
        assert b" 401 " in head.split(b"\r\n")[0]
        assert b"WWW-Authenticate: Basic" in head
    _, _, head = await _request(srv.port, "", user, pw)
    assert b" 401 " in head
    assert hub.auth_failures == 3 and hub.client_count() == 0


@pytest.mark.asyncio
async def test_malformed_and_wrong_method(ntrip):
    _, srv = ntrip
    for raw in (b"garbage\r\n\r\n", b"GET /1001\r\n\r\n", b"X" * 9000):
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(raw)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 5) == b""

    reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
    writer.write(b"POST /1001 HTTP/1.1\r\nNtrip-Version: Ntrip/2.0\r\n\r\n")
    assert (await asyncio.wait_for(reader.read(), 5)).startswith(b"HTTP/1.1 405")


@pytest.mark.asyncio
async def test_client_limit(ntrip):
    hub, srv = ntrip
    hub.max_clients = 1
    RawOutSink(hub, 1001, "1001")
    _, first, head = await _request(srv.port, "1001", "proc", "s3cret")
    assert head == b"ICY 200 OK\r\n"
    await _wait_clients(hub, 1)
    _, _, head = await _request(srv.port, "1001", "proc", "s3cret")
    assert b" 503 " in head
    first.close()


@pytest.mark.asyncio
async def test_client_gga_is_ignored(ntrip):
    hub, srv = ntrip
    RawOutSink(hub, 1001, "1001")
    reader, writer, _ = await _request(srv.port, "1001", "proc", "s3cret")
    await _wait_clients(hub, 1)
    writer.write(b"$GPGGA,120000.00,4724.000,N,00830.000,E,1,10,1.0,500.0,M,48.0,M,,*00\r\n")
    await writer.drain()
    frame = rawx(WEEK, TOW)
    hub.publish(1001, frame)
    assert await _read_exact(reader, len(frame)) == frame
    writer.close()


# --------------------------------------------------------------------------
# a slow or broken client costs only itself
# --------------------------------------------------------------------------
class _FakeTransport:
    def __init__(self):
        self.buffer = 0
        self.aborted = False

    def get_write_buffer_size(self):
        return self.buffer

    def abort(self):
        self.aborted = True


class _FakeWriter:
    def __init__(self, fail=False):
        self.transport = _FakeTransport()
        self.data = bytearray()
        self.fail = fail

    def write(self, b):
        if self.fail:
            raise ConnectionResetError("gone")
        self.data += b


def _client(hub, writer, user="proc"):
    c = rawout._Client(user, 1001, "peer", 1, writer)
    hub.add(c)
    return c


def test_slow_client_is_cut_off_others_continue():
    hub = RawOutHub(dict(ACCOUNTS), buffer_limit=1000, max_clients=4)
    RawOutSink(hub, 1001, "1001")
    slow, fast = _FakeWriter(), _FakeWriter()
    _client(hub, slow)
    _client(hub, fast)

    hub.publish(1001, b"a" * 10)
    slow.transport.buffer = 1001            # the slow consumer stops reading
    hub.publish(1001, b"b" * 10)
    hub.publish(1001, b"c" * 10)

    assert slow.transport.aborted and not fast.transport.aborted
    assert bytes(fast.data) == b"a" * 10 + b"b" * 10 + b"c" * 10
    assert bytes(slow.data) == b"a" * 10 + b"b" * 10     # cut right after crossing
    st = hub.status()["stations"]["1001"]
    assert st["dropped_slow"] == 1 and st["clients"] == 1


def test_broken_client_never_raises_into_routing():
    hub = RawOutHub(dict(ACCOUNTS), buffer_limit=1000, max_clients=4)
    sink = RawOutSink(hub, 1001, "1001")
    broken, good = _FakeWriter(fail=True), _FakeWriter()
    _client(hub, broken)
    _client(hub, good)
    sink.on_ubx(b"x", None, False)          # must not raise
    sink.on_rtcm3(b"y", None, False)
    assert broken.transport.aborted
    assert bytes(good.data) == b"xy"
    assert hub.client_count() == 1


def test_publish_without_clients_is_free():
    hub = RawOutHub({}, 1000, 4)
    hub.publish(1001, b"x")                 # unknown station, no clients
    RawOutSink(hub, 1001, "1001").on_ubx(b"x", None, False)
    assert hub.status()["stations"]["1001"]["bytes_out"] == 0


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
async def test_tls(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True)
    hub = RawOutHub(dict(ACCOUNTS), 1 << 20, 4)
    RawOutSink(hub, 1001, "1001")
    srv = RawOutServer(hub, "127.0.0.1", 0, rawout.make_ssl_context(cert, key))
    await srv.start()
    try:
        ctx = ssl.create_default_context(cafile=str(cert))
        reader, writer, head = await _request(srv.port, "1001", "proc", "s3cret",
                                              v2=True, ssl_ctx=ctx)
        assert head.startswith(b"HTTP/1.1 200 OK")
        await _wait_clients(hub, 1)
        frame = rawx(WEEK, TOW)
        hub.publish(1001, frame)
        assert await _read_chunk(reader) == frame
        writer.close()

        # Plain text against the TLS port gets nothing useful back.
        r, w = await asyncio.open_connection("127.0.0.1", srv.port)
        w.write(b"GET /1001 HTTP/1.0\r\n" + _auth("proc", "s3cret").encode() + b"\r\n")
        await w.drain()
        assert b"ICY" not in await asyncio.wait_for(r.read(), 5)
    finally:
        await srv.close()
