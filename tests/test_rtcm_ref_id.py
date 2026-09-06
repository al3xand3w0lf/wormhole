"""Filling DF003 in for receivers that leave it at 0.

The invariant worth guarding is not "a byte changed" but that the rewrite is
*narrow*: it touches the reference station id and nothing else, only on message
types that actually carry one, only when the receiver named no station, and it
leaves a frame that still verifies. Everything downstream — archive, caster push,
in-fleet rover router — reads the same rewritten frame, so a mistake here is a
mistake in the corrections themselves.
"""

import io

import pytest
from pyrtcm import RTCMReader

from streaming import rtcm
from streaming.framer import KIND_RTCM3, Frame
from streaming.pipeline import route_frame
from streaming.sinks import Sink
from streaming.station import StationSession

from .helpers import rtcm3, rtcm3_1005

BASE_ECEF = (4279559.7844, 643304.3816, 4670269.3628)


def _typed(msg_type: int, ref_id: int, payload_len: int = 20) -> bytes:
    """A frame of `msg_type` carrying `ref_id`, rest zeroed."""
    bits = format(msg_type, "012b") + format(ref_id, "012b")
    head = int(bits, 2).to_bytes(3, "big")
    return rtcm3(head + b"\x00" * (payload_len - 3))


def _frame(raw: bytes) -> Frame:
    return Frame(kind=KIND_RTCM3, raw=raw, cls_=None, id_=None)


class _Collect(Sink):
    def __init__(self):
        self.frames: list[bytes] = []

    def on_rtcm3(self, raw, stamp, sysclk):
        self.frames.append(raw)


def _session(station_id: int):
    sink = _Collect()
    return StationSession(station_id, [sink]), sink


# ------------------------------------------------------------------ reading


def test_reads_the_id_the_same_way_pyrtcm_does():
    """The whole rewrite rests on DF003 sitting at payload bits 12..23. Anchor
    that against a real decoder rather than against our own arithmetic."""
    raw = rtcm3_1005(1009, *BASE_ECEF)
    (_, parsed), = [(r, p) for r, p in RTCMReader(io.BytesIO(raw))]
    assert rtcm.reference_station_id(raw) == parsed.DF003 == 1009
    assert rtcm.message_type(raw) == int(parsed.identity) == 1005


def test_types_without_a_reference_station_id_read_as_none():
    """1019 is a GPS ephemeris: those bits are a satellite id, not a station."""
    assert rtcm.reference_station_id(_typed(1019, 7)) is None
    assert rtcm.reference_station_id(_typed(1042, 7)) is None
    assert rtcm.reference_station_id(_typed(4072, 7)) is None  # u-blox proprietary


def test_a_truncated_frame_reads_as_none_instead_of_raising():
    assert rtcm.reference_station_id(b"\xd3\x00\x01\x00") is None
    assert rtcm.message_type(b"\xd3") == -1


# ------------------------------------------------------------------ writing


def test_rewrite_changes_only_the_id_and_still_verifies():
    original = rtcm3_1005(0, *BASE_ECEF)
    patched = rtcm.set_reference_station_id(original, 1009)

    assert len(patched) == len(original)
    # Everything but the two id-bearing bytes and the CRC is untouched.
    assert patched[:4] == original[:4]
    assert patched[6:-3] == original[6:-3]

    # RTCMReader with quitonerror=2 raises on a bad CRC, so a clean parse is
    # the checksum assertion.
    (_, parsed), = [(r, p) for r, p in RTCMReader(io.BytesIO(patched), quitonerror=2)]
    assert parsed.DF003 == 1009
    assert (round(parsed.DF025, 4), round(parsed.DF026, 4), round(parsed.DF027, 4)) == BASE_ECEF


def test_rewrite_refuses_an_id_too_wide_for_the_field():
    """4096 truncates to 0 in 12 bits - i.e. to "no station named", the exact
    thing this module exists to remove. Refuse instead."""
    with pytest.raises(ValueError):
        rtcm.set_reference_station_id(rtcm3_1005(0, *BASE_ECEF), 4096)
    with pytest.raises(ValueError):
        rtcm.set_reference_station_id(rtcm3_1005(0, *BASE_ECEF), -1)


# ------------------------------------------------------------------ routing


def test_a_zero_id_is_filled_in_with_the_station_number():
    session, sink = _session(1009)
    route_frame(session, _frame(rtcm3_1005(0, *BASE_ECEF)))

    assert rtcm.reference_station_id(sink.frames[0]) == 1009
    assert session.ref_id_filled is True
    assert session.status()["ref_id_filled"] is True


def test_an_id_the_receiver_set_itself_is_left_alone():
    """The self-disabling property: a device that names itself is passed through
    byte-for-byte, so this needs no per-station configuration and no opt-out."""
    session, sink = _session(1001)
    original = rtcm3_1005(1001, *BASE_ECEF)
    route_frame(session, _frame(original))

    assert sink.frames[0] == original
    assert session.ref_id_filled is False
    assert session.status()["ref_id_filled"] is False


def test_a_foreign_id_is_never_overwritten():
    """Only 0 means "unnamed". A frame claiming another station is a fault to
    leave visible, not to paper over with our own number."""
    session, sink = _session(1009)
    original = rtcm3_1005(1234, *BASE_ECEF)
    route_frame(session, _frame(original))

    assert sink.frames[0] == original
    assert session.ref_id_filled is False


def test_an_ephemeris_message_is_passed_through_untouched():
    """Bits 12..23 there are a satellite id; rewriting them would corrupt the
    ephemeris while leaving a frame that still checksums cleanly."""
    session, sink = _session(1009)
    original = _typed(1019, 0)
    route_frame(session, _frame(original))

    assert sink.frames[0] == original
    assert session.ref_id_filled is False


def test_every_message_type_the_base_actually_sends_gets_the_id():
    """1005 + MSM7 + 1230 is the set a configured base emits; all of them carry
    DF003, so a rover sees one consistent id no matter which message it reads."""
    session, sink = _session(1009)
    for msg_type in (1005, 1077, 1087, 1097, 1127, 1230):
        route_frame(session, _frame(_typed(msg_type, 0)))

    assert [rtcm.reference_station_id(f) for f in sink.frames] == [1009] * 6


def test_an_id_too_wide_leaves_the_stream_untouched():
    session, sink = _session(70001)
    original = rtcm3_1005(0, *BASE_ECEF)
    route_frame(session, _frame(original))

    assert sink.frames[0] == original
    assert session.ref_id_filled is False


def test_the_fill_is_reported_once_not_per_frame(caplog):
    session, _ = _session(1009)
    with caplog.at_level("INFO", logger="streaming"):
        for _ in range(5):
            route_frame(session, _frame(rtcm3_1005(0, *BASE_ECEF)))

    assert sum("DF003=0" in r.getMessage() for r in caplog.records) == 1


def test_the_histogram_still_counts_the_type_it_saw():
    session, _ = _session(1009)
    route_frame(session, _frame(rtcm3_1005(0, *BASE_ECEF)))
    assert session.rtcm3_types == {1005: 1}
