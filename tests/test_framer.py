"""The framer must survive a damaged stream: a device can drop bytes when its tee
buffer fills, or tear a frame in two by writing something else between the chunks of
a chunked send. Either way the framer has to resync, not desync — desyncing on a
single bad frame would silently lose the rest of the session.
"""

from pyubx2 import UBXReader

from streaming.framer import KIND_RTCM3, KIND_UBX, StreamFramer

from .helpers import cli_response, ident, rawx, rtcm3, ubx


def frames(data: bytes, framer: StreamFramer | None = None) -> list:
    framer = framer or StreamFramer()
    return list(framer.feed(data))


def test_single_ubx():
    out = frames(rawx(2378, 100000.0))
    assert len(out) == 1
    assert out[0].kind == KIND_UBX
    assert (out[0].cls_, out[0].id_) == (0x02, 0x15)


def test_single_rtcm3():
    out = frames(rtcm3(b"\x3e\xd0" + b"\x11" * 20))
    assert len(out) == 1
    assert out[0].kind == KIND_RTCM3


def test_raw_bytes_are_byte_exact():
    msg = rawx(2378, 100000.0)
    out = frames(msg)
    assert out[0].raw == msg


def test_framed_ubx_is_accepted_by_pyubx2():
    """Cross-check: what we hand to the sinks must be parseable by the library."""
    out = frames(rawx(2378, 100000.0))
    parsed = UBXReader.parse(out[0].raw)
    assert parsed.identity == "RXM-RAWX"
    assert parsed.week == 2378
    assert parsed.leapS == 18


def test_interleaved_stream():
    stream = ident(1001) + rawx(2378, 1.0) + rtcm3(b"\xff" * 10) + cli_response("hi", True)
    out = frames(stream)
    assert [f.kind for f in out] == [KIND_UBX, KIND_UBX, KIND_RTCM3, KIND_UBX]


def test_leading_garbage_is_discarded():
    framer = StreamFramer()
    out = frames(b"\x01\x02\x03garbage" + rawx(2378, 1.0), framer)
    assert len(out) == 1
    assert framer.garbage_bytes == 10
    assert framer.resync_events >= 1


def test_truncated_frame_then_valid_frame_resyncs():
    good = rawx(2378, 1.0)
    truncated = good[:-4]  # device dropped the tail
    framer = StreamFramer()
    out = frames(truncated + good, framer)
    assert len(out) == 1
    assert out[0].raw == good
    assert framer.resync_events >= 1


def test_bad_checksum_is_rejected_and_resyncs():
    good = rawx(2378, 1.0)
    corrupt = bytearray(good)
    corrupt[-1] ^= 0xFF  # break the checksum
    framer = StreamFramer()
    out = frames(bytes(corrupt) + good, framer)
    assert len(out) == 1
    assert out[0].raw == good


def test_bad_rtcm3_crc_is_rejected():
    good = rtcm3(b"\xaa" * 16)
    corrupt = bytearray(good)
    corrupt[-1] ^= 0xFF
    framer = StreamFramer()
    out = frames(bytes(corrupt) + good, framer)
    assert len(out) == 1
    assert out[0].raw == good


def test_frame_split_across_feeds():
    msg = rawx(2378, 1.0)
    framer = StreamFramer()
    collected = []
    for i in range(0, len(msg), 3):  # dribble it in 3-byte chunks
        collected += list(framer.feed(msg[i : i + 3]))
    assert len(collected) == 1
    assert collected[0].raw == msg


def test_sync_byte_inside_payload_does_not_confuse():
    # A payload containing 0xB5 / 0xD3 must not break framing.
    payload = bytes([0xB5, 0x62, 0xD3, 0x00]) * 8
    msg = ubx(0x02, 0x13, payload)
    out = frames(msg + rawx(2378, 1.0))
    assert len(out) == 2


def test_absurd_length_is_rejected():
    # 0xB5 0x62 with a huge declared length must not stall the framer.
    bogus = b"\xb5\x62\x01\x02\xff\xff"
    framer = StreamFramer()
    out = frames(bogus + rawx(2378, 1.0), framer)
    assert len(out) == 1
    assert framer.resync_events >= 1


def test_rtcm3_reserved_bits_must_be_zero():
    framer = StreamFramer()
    # 0xD3 followed by a byte with reserved bits set is not a real RTCM3 header.
    out = frames(b"\xd3\xfc\x10" + rawx(2378, 1.0), framer)
    assert len(out) == 1
    assert framer.resync_events >= 1


def test_counters():
    framer = StreamFramer()
    frames(rawx(2378, 1.0) + rtcm3(b"\x00" * 8), framer)
    assert framer.ubx_frames == 1
    assert framer.rtcm3_frames == 1


def test_pure_garbage_never_stalls():
    framer = StreamFramer()
    out = frames(bytes(range(256)) * 400, framer)
    assert out == []
    # buffer must not grow without bound
    assert len(framer._buf) <= framer._max_buffer
