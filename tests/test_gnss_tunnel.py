"""GNSS maintenance tunnel (streaming/gnss_tunnel.py), K-01 Etappe 2.

What is worth testing here is narrow but unforgiving. The tunnel carries a
firmware image to a receiver that cannot be reached any other way, so the only
interesting properties are the ones whose failure is silent:

  * the envelope round-trips BYTE FOR BYTE, including 0xFF runs (an erased-flash
    region is nothing but 0xFF, so a transparency bug would corrupt exactly the
    empty parts of an image and nothing else);
  * the Telnet/RFC2217 filter removes what a virtual COM port injects and
    nothing else, at any read boundary;
  * chunking never silently splits or truncates;
  * a frame arriving with no tunnel open is dropped rather than raising.

The bench numbers quoted in the comments come from measurements of the device's
serial bridge.
"""

import asyncio

import pytest

from streaming import gnss_tunnel
from streaming.frames import (
    ID_GNSS_TUNNEL_DOWN,
    ID_GNSS_TUNNEL_UP,
    PRIVATE_CLASS,
    TUNNEL_MAX_PAYLOAD,
    GnssTunnelData,
    build_ubx,
    decode_private,
    encode_gnss_tunnel,
    ubx_payload,
)
from streaming.framer import StreamFramer
from streaming.gnss_tunnel import (GnssTunnel, TunnelRegistry, escape_telnet,
                                   strip_telnet)


# -- the envelope contract --------------------------------------------------

def test_downlink_envelope_round_trips():
    raw = bytes(range(256)) * 2          # 512 B, every byte value twice
    env = encode_gnss_tunnel(raw)

    frames = list(StreamFramer().feed(env))
    assert len(frames) == 1
    assert frames[0].cls_ == PRIVATE_CLASS
    assert frames[0].id_ == ID_GNSS_TUNNEL_DOWN
    assert ubx_payload(frames[0].raw) == raw


def test_uplink_decodes_to_raw_bytes():
    raw = b"\xb5\x62\x0a\x04garbage\x00\xff"
    msg = decode_private(build_ubx(PRIVATE_CLASS, ID_GNSS_TUNNEL_UP, raw))
    assert isinstance(msg, GnssTunnelData)
    assert msg.data == raw


def test_all_0xff_payload_survives():
    """An erased flash sector is 0xFF end to end.

    This is the payload most likely to be mangled by any layer that treats 0xFF
    specially - which the Telnet filter does, one layer up. If transparency ever
    breaks here it breaks *only* on erased regions, i.e. on an image that flashes
    "almost" correctly.
    """
    raw = b"\xff" * TUNNEL_MAX_PAYLOAD
    frames = list(StreamFramer().feed(encode_gnss_tunnel(raw)))
    assert ubx_payload(frames[0].raw) == raw


def test_oversized_chunk_is_refused_not_truncated():
    """Refusing is the point: the device reassembles into a fixed buffer, and a
    silently split chunk would put the chunking rule in two places at once."""
    with pytest.raises(ValueError):
        encode_gnss_tunnel(b"\x00" * (TUNNEL_MAX_PAYLOAD + 1))


def test_empty_chunk_is_refused():
    with pytest.raises(ValueError):
        encode_gnss_tunnel(b"")


# -- the Telnet / RFC 2217 filter -------------------------------------------
#
# The sequence below is the literal capture from the bench on 2026-09-04. It
# arrived between the boot ROM's CRC poll and its answer; the tool reported
# "Could not get ROM CRC" and the rescue failed.

BENCH_CAPTURE = bytes.fromhex("fffa2c0100002580fff0")


def test_bench_telnet_sequence_is_removed():
    state = {}
    assert strip_telnet(BENCH_CAPTURE, state) == b""


def test_telnet_removal_preserves_surrounding_data():
    state = {}
    data = b"\x01\x02" + BENCH_CAPTURE + b"\x03\x04"
    assert strip_telnet(data, state) == b"\x01\x02\x03\x04"


def test_doubled_iac_becomes_one_literal_ff():
    """`FF FF` on the wire is one real 0xFF byte. Getting this wrong corrupts
    erased-flash regions specifically - see test_all_0xff_payload_survives."""
    state = {}
    assert strip_telnet(b"\xff\xff", state) == b"\xff"


def test_run_of_real_ff_bytes_survives_the_filter():
    state = {}
    escaped = b"\xff\xff" * 64          # 64 real 0xFF bytes, as a driver sends them
    assert strip_telnet(escaped, state) == b"\xff" * 64


def test_filter_is_stable_across_every_read_boundary():
    """A sequence can straddle a read. Splitting at every position must give the
    same answer - a filter that is only correct on aligned reads is a filter that
    fails under load and nowhere else."""
    data = b"\x01\x02" + BENCH_CAPTURE + b"\x03\x04"
    for i in range(len(data) + 1):
        state = {}
        out = strip_telnet(data[:i], state) + strip_telnet(data[i:], state)
        assert out == b"\x01\x02\x03\x04", f"split at {i}"


def test_plain_binary_passes_through_untouched():
    state = {}
    ubx = b"\xb5\x62\x0a\x04\x00\x00\x0e\x34"
    assert strip_telnet(ubx, state) == ubx


# -- session plumbing -------------------------------------------------------

class FakeWriter:
    def __init__(self):
        self.buf = bytearray()
        self.closed = False

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    def get_extra_info(self, _name):
        return ("127.0.0.1", 0)


class FakeSession:
    def __init__(self, station_id=1001):
        self.station_id = station_id
        self.writer = FakeWriter()
        self.write_lock = asyncio.Lock()


@pytest.mark.asyncio
async def test_send_to_device_chunks_without_loss():
    """2.5 envelopes' worth: the bytes must arrive in order, complete, and split
    only at the envelope boundary."""
    session = FakeSession()
    tunnel = GnssTunnel(session)
    payload = bytes((i * 7) & 0xFF for i in range(TUNNEL_MAX_PAYLOAD * 2 + 500))

    await tunnel._send_to_device(payload)

    frames = list(StreamFramer().feed(bytes(session.writer.buf)))
    assert len(frames) == 3
    assert all(f.id_ == ID_GNSS_TUNNEL_DOWN for f in frames)
    assert b"".join(ubx_payload(f.raw) for f in frames) == payload
    assert tunnel.stats.to_device == len(payload)


@pytest.mark.asyncio
async def test_send_to_device_raises_when_station_dropped():
    session = FakeSession()
    session.writer = None
    tunnel = GnssTunnel(session)
    with pytest.raises(ConnectionError):
        await tunnel._send_to_device(b"hello")


def test_feed_from_device_without_operator_is_silent():
    """Between a session ending and the receiver going quiet there is always a
    tail of uplink frames. Dropping them is correct; raising would take down the
    station's whole frame loop."""
    tunnel = GnssTunnel(FakeSession())
    tunnel.feed_from_device(b"\x01\x02\x03")        # must not raise
    assert tunnel.stats.to_operator == 0


def test_feed_from_device_forwards_verbatim():
    tunnel = GnssTunnel(FakeSession())
    op = FakeWriter()
    tunnel._operator_writer = op
    tunnel.feed_from_device(b"\xff\x00\xb5\x62")
    assert bytes(op.buf) == b"\xff\x00\xb5\x62"
    assert tunnel.stats.to_operator == 4


def test_escape_telnet_is_the_inverse_of_strip():
    """Whatever we double on the way out, strip_telnet() must halve on the way
    in. The two halves of the same rule lived apart until 2026-09-21, and only
    one of them existed."""
    payload = bytes([0xFF, 0x00, 0xFF, 0xFF, 0xB5, 0x62, 0xFF])
    assert strip_telnet(escape_telnet(payload), {}) == payload


def test_escape_telnet_leaves_clean_data_untouched():
    assert escape_telnet(b"\xb5\x62\x01\x07") == b"\xb5\x62\x01\x07"


def test_feed_from_device_escapes_for_a_telnet_operator():
    """A lone 0xFF reaching an RFC2217 driver reads as IAC and eats the byte
    after it. The receiver's answers are full of 0xFF and ubxfwupdate waits on
    exactly those bytes."""
    tunnel = GnssTunnel(FakeSession())
    op = FakeWriter()
    tunnel._operator_writer = op
    tunnel._operator_telnet = True
    tunnel.feed_from_device(b"\xff\x00\xb5\x62")
    assert bytes(op.buf) == b"\xff\xff\x00\xb5\x62"
    assert tunnel.stats.telnet_escaped == 1


def test_feed_from_device_counts_payload_not_wire():
    """to_operator is compared byte for byte with the DEVICE's own toPc. If
    Telnet framing were counted, a correct transfer would look like a mismatch -
    and that comparison is the only proof this path keeps its promise."""
    tunnel = GnssTunnel(FakeSession())
    op = FakeWriter()
    tunnel._operator_writer = op
    tunnel._operator_telnet = True
    tunnel.feed_from_device(b"\xff\xff\xff")
    assert tunnel.stats.to_operator == 3           # not 6
    assert len(bytes(op.buf)) == 6


def test_feed_from_device_never_escapes_for_a_raw_operator():
    """The dangerous direction of the same decision: doubling 0xFF for a peer
    that never negotiated corrupts a firmware image exactly where it is erased."""
    tunnel = GnssTunnel(FakeSession())
    op = FakeWriter()
    tunnel._operator_writer = op
    tunnel._operator_telnet = False
    tunnel.feed_from_device(b"\xff\xff\xff")
    assert bytes(op.buf) == b"\xff\xff\xff"
    assert tunnel.stats.telnet_escaped == 0


def test_escaping_decision_is_per_connection_not_per_session():
    """stats.telnet_mode remembers the session's last peer for the status view.
    The wire must follow the CURRENT one: a Telnet operator followed by a raw
    one must not leave the raw one with a doubled stream."""
    tunnel = GnssTunnel(FakeSession())
    tunnel.stats.telnet_mode = True                # a previous operator
    op = FakeWriter()
    tunnel._operator_writer = op
    tunnel._operator_telnet = None                 # this one has not spoken yet
    tunnel.feed_from_device(b"\xff\xff")
    assert bytes(op.buf) == b"\xff\xff"


# -- the registry -----------------------------------------------------------

@pytest.mark.asyncio
async def test_registry_is_one_tunnel_per_station():
    reg = TunnelRegistry()
    session = FakeSession(1001)
    try:
        first = await reg.open(session)
        second = await reg.open(session)
        # Not a new listener: two tools on one receiver interleave their frames
        # and neither succeeds.
        assert first is second
        assert reg.get(1001) is first
    finally:
        await reg.close(1001)
    assert reg.get(1001) is None


@pytest.mark.asyncio
async def test_registry_close_of_unknown_station_is_false_not_an_error():
    reg = TunnelRegistry()
    assert await reg.close(4242) is False


@pytest.mark.asyncio
async def test_listener_binds_loopback_only():
    """A "temporarily open" port stays open. The operator reaches this through an
    SSH forward, so it has no business on a public interface."""
    reg = TunnelRegistry()
    session = FakeSession(1002)
    try:
        tunnel = await reg.open(session, "127.0.0.1", 0)
        assert tunnel.port != 0
        host, _port = tunnel._server.sockets[0].getsockname()[:2]
        assert host == "127.0.0.1"
    finally:
        await reg.close(1002)


@pytest.mark.asyncio
async def test_operator_bytes_reach_the_station_socket():
    """The one test that exercises the whole local half: a socket client writes,
    and the bytes come out enveloped on the station's writer."""
    reg = TunnelRegistry()
    session = FakeSession(1003)
    try:
        tunnel = await reg.open(session, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", tunnel.port)
        writer.write(b"\xb5\x62\x0a\x04\x00\x00")
        await writer.drain()

        for _ in range(100):                     # let the server task run
            if session.writer.buf:
                break
            await asyncio.sleep(0.01)

        frames = list(StreamFramer().feed(bytes(session.writer.buf)))
        assert len(frames) == 1
        assert ubx_payload(frames[0].raw) == b"\xb5\x62\x0a\x04\x00\x00"

        writer.close()
    finally:
        await reg.close(1003)


@pytest.mark.asyncio
async def test_second_operator_is_refused():
    reg = TunnelRegistry()
    session = FakeSession(1004)
    try:
        tunnel = await reg.open(session, "127.0.0.1", 0)
        _r1, w1 = await asyncio.open_connection("127.0.0.1", tunnel.port)
        for _ in range(100):
            if tunnel._operator_writer is not None:
                break
            await asyncio.sleep(0.01)
        assert tunnel._operator_writer is not None

        r2, w2 = await asyncio.open_connection("127.0.0.1", tunnel.port)
        # The server closes the second connection without sending anything.
        assert await r2.read(1) == b""
        assert tunnel.stats.operator_connects == 1

        w1.close()
        w2.close()
    finally:
        await reg.close(1004)
