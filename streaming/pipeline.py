"""Frame routing — shared by the live TCP server and the offline replay tool.

Keeping this in one place is what makes `replay.py` meaningful: a recorded raw
stream is processed by exactly the same code path as a live connection.
"""

import logging
from datetime import datetime, timezone

from pyubx2 import UBXReader

from .frames import (
    PRIVATE_CLASS,
    ID_IDENT,
    CliResponse,
    Heartbeat,
    Ident,
    SensorReading,
    decode_private,
)
from .framer import KIND_RTCM3, Frame
from .station import StationSession

logger = logging.getLogger("streaming")

# UBX-RXM-RAWX — the device's time source (and ours).
RXM_CLASS = 0x02
RXM_RAWX_ID = 0x15


def is_ident(frame: Frame) -> bool:
    return frame.cls_ == PRIVATE_CLASS and frame.id_ == ID_IDENT


def rtcm3_message_type(raw: bytes) -> int:
    """The RTCM3 message number (DF002) — the first 12 bits of the payload.

    Read directly rather than via a full pyrtcm decode: we only record RTCM3 raw,
    and a *type histogram* is all we need. It is also what tells you at a glance
    whether the base is actually emitting 1005 + MSM7 + 1230, i.e. whether the
    device's TMODE3/RTCM-MSGOUT configuration took effect.
    """
    if len(raw) < 5:
        return -1
    return (raw[3] << 4) | (raw[4] >> 4)


def ident_station_id(frame: Frame) -> int | None:
    msg = decode_private(frame.raw)
    return msg.station_id if isinstance(msg, Ident) else None


def _update_clock(session: StationSession, raw: bytes) -> None:
    """Feed RXM-RAWX into the station's GPS clock.

    Field extraction via pyubx2; the GPS->calendar conversion is ours (it must
    mirror the firmware's leap-second-free arithmetic — see gpstime.py).
    """
    try:
        msg = UBXReader.parse(raw, parsebitfield=0)
    except Exception as exc:  # noqa: BLE001 - a corrupt frame must never kill the loop
        logger.debug("RAWX parse failed: %s", exc)
        return

    week = getattr(msg, "week", None)
    tow = getattr(msg, "rcvTow", None)
    if week is None or tow is None:
        return
    leap = getattr(msg, "leapS", 0) or 0

    was_valid = session.clock.valid
    if session.clock.update_from_rawx(week, tow, leap) and not was_valid:
        logger.info(
            "station %s GPS clock acquired: %s (leapS=%s)",
            session.station_id,
            session.clock.gps_time,
            session.clock.leap_s,
        )


def route_frame(session: StationSession, frame: Frame) -> None:
    """Route one validated frame to the station's sinks."""
    session.last_frame_at = datetime.now(timezone.utc)

    if frame.kind == KIND_RTCM3:
        session.rtcm3_frames += 1
        session.note_rtcm3_type(rtcm3_message_type(frame.raw))
        stamp, sysclk = session.stamp()
        for sink in session.sinks:
            sink.on_rtcm3(frame.raw, stamp, sysclk)
        return

    # --- UBX ---
    if frame.cls_ == PRIVATE_CLASS:
        session.private_frames += 1
        _route_private(session, frame)
        return

    session.ubx_frames += 1

    # Update the clock *before* writing, so this frame already lands in the
    # correct hourly file.
    if frame.cls_ == RXM_CLASS and frame.id_ == RXM_RAWX_ID:
        _update_clock(session, frame.raw)

    stamp, sysclk = session.stamp()
    for sink in session.sinks:
        sink.on_ubx(frame.raw, stamp, sysclk)


def _route_private(session: StationSession, frame: Frame) -> None:
    msg = decode_private(frame.raw)
    if msg is None:
        logger.debug("station %s: undecodable private frame id=0x%02X", session.station_id, frame.id_)
        return

    if isinstance(msg, SensorReading):
        for sink in session.sinks:
            sink.on_sensor(msg, session.clock)
        return

    if isinstance(msg, CliResponse):
        session.handle_cli_response(msg)
        return

    if isinstance(msg, (Heartbeat, Ident)):
        # IDENT is consumed by the connection handler; a repeat is harmless.
        return
