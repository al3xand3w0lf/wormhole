"""Frame routing — shared by the live TCP server and the offline replay tool.

Keeping this in one place is what makes `replay.py` meaningful: a recorded raw
stream is processed by exactly the same code path as a live connection.
"""

import asyncio
import logging
from datetime import datetime, timezone

from pyubx2 import UBXReader

from . import config, filetransfer
from .frames import (
    PRIVATE_CLASS,
    ID_IDENT,
    FILE_PHASE_ABORTED,
    FILE_PHASE_DONE,
    CliResponse,
    FileRequest,
    FileStatus,
    FileUpBegin,
    FileUpData,
    Heartbeat,
    Ident,
    NmeaSentence,
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


def decode_ident(frame: Frame) -> Ident | None:
    """The full IDENT, including its role byte - see frames.py's Ident.role.

    Kept separate from ident_station_id() rather than replacing it: that
    helper is the narrower, older contract (station id only) and this avoids
    touching any caller that only ever needed that.
    """
    msg = decode_private(frame.raw)
    return msg if isinstance(msg, Ident) else None


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

    if isinstance(msg, NmeaSentence):
        stamp, sysclk = session.stamp()
        for sink in session.sinks:
            sink.on_nmea(msg.text, stamp, sysclk)
        return

    if isinstance(msg, CliResponse):
        session.handle_cli_response(msg)
        return

    if isinstance(msg, FileRequest):
        _start_file_transfer(session, msg)
        return

    if isinstance(msg, FileUpBegin):
        _upload_receiver(session).begin(msg)
        return

    if isinstance(msg, FileUpData):
        _upload_receiver(session).data(msg)
        return

    if isinstance(msg, FileStatus):
        _log_file_status(session, msg)
        return

    if isinstance(msg, (Heartbeat, Ident)):
        # IDENT is consumed by the connection handler; a repeat is harmless.
        return


def _start_file_transfer(session: StationSession, req: FileRequest) -> None:
    """Kick off the transfer as a background task.

    Deliberately fire-and-forget on the event loop: route_frame() is synchronous
    and is also driven by replay.py, which has no loop at all. A recorded stream
    replayed offline therefore logs the request and moves on instead of trying to
    serve a file to a device that is not there.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.info("station %s: FILE_REQUEST %r (offline replay - not served)",
                    session.station_id, req.name)
        return

    # Keep a reference: a bare create_task() may be garbage-collected mid-flight.
    session.file_transfer_task = loop.create_task(filetransfer.send_file(session, req.name))


def _upload_receiver(session: StationSession) -> filetransfer.UploadReceiver:
    """The station's upload reassembler, created on first use.

    Lazy so that a station that never uploads never touches the filesystem, and
    so replay.py can route these frames without a live connection.
    """
    if session.upload_receiver is None:
        session.upload_receiver = filetransfer.UploadReceiver(
            config.STREAM_DIR, session.station_id)
    return session.upload_receiver


def _log_file_status(session: StationSession, st: FileStatus) -> None:
    if st.phase == FILE_PHASE_DONE:
        logger.info("station %s: transfer complete, %d bytes", session.station_id, st.bytes_)
    elif st.phase == FILE_PHASE_ABORTED:
        logger.warning("station %s: transfer aborted after %d bytes (device code %d)",
                       session.station_id, st.bytes_, st.code)
        session.fail_pending_download(f"transfer aborted (device code {st.code})")
    else:
        logger.info("station %s: transfer accepted, %d bytes expected",
                    session.station_id, st.bytes_)
