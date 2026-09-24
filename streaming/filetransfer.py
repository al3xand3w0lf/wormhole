"""Serving files to a device over its streaming socket.

The device asks with FILE_REQUEST, we answer FILE_BEGIN (total length + CRC32)
and then write exactly that many RAW bytes into the same connection. No envelope
around the payload and no per-chunk header: `AT+QIRD` is length-prefixed on the
device side, so the modem already tells the firmware how many bytes each read
carries. That is what lets a receiver without a frame parser consume a 330 KB
firmware image safely.

Flow control is TCP's. The modem buffers a bounded amount and only drains it when
the firmware issues QIRD, so `await writer.drain()` blocks here exactly as long
as the device is behind — no explicit windowing needed.

Only one transfer per station at a time: the device pauses its GNSS uplink for
the duration and runs the whole thing on the task that owns the socket, so a
second concurrent transfer has nothing to run on.
"""

import asyncio
import logging
import zlib
from datetime import datetime, timezone
from pathlib import Path

from downloads import resolve_download, sanitize_filename

from . import stationdir
from .frames import (
    UP_ACK_ABORTED,
    UP_ACK_CRC_MISMATCH,
    UP_ACK_OK,
    UP_ACK_SIZE_MISMATCH,
    FileUpBegin,
    FileUpData,
    encode_file_begin,
    encode_file_up_ack,
)

logger = logging.getLogger("streaming")

# Bytes per write into the socket. Small enough that drain() reacts promptly to
# a device that stops reading, large enough not to syscall per kilobyte.
CHUNK = 8192


def _crc32_of(path) -> tuple[int, int]:
    """(size, crc32) of a file, read once."""
    crc = 0
    size = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            size += len(block)
            crc = zlib.crc32(block, crc)
    return size, crc & 0xFFFFFFFF


async def send_file(session, name: str) -> None:
    """Answer one FILE_REQUEST. Never raises — a failure is reported to the log
    and, where possible, to the device as `total = 0`."""
    writer = session.writer
    if writer is None:
        logger.warning("station %s: FILE_REQUEST %r but not connected", session.station_id, name)
        return

    if session.file_transfer_active:
        logger.warning("station %s: FILE_REQUEST %r while a transfer runs - ignored",
                       session.station_id, name)
        return

    loop = asyncio.get_running_loop()
    try:
        safe_name, path = await loop.run_in_executor(None, resolve_download, name)
    except Exception as exc:  # noqa: BLE001 - never kill the connection handler
        logger.error("station %s: cannot resolve %r: %s", session.station_id, name, exc)
        safe_name, path = name, None

    if path is None:
        logger.info("station %s: FILE_REQUEST %r -> not found", session.station_id, name)
        writer.write(encode_file_begin(0, 0, safe_name))
        await writer.drain()
        session.fail_pending_download(f"{safe_name}: not found on server")
        return

    try:
        size, crc = await loop.run_in_executor(None, _crc32_of, path)
    except OSError as exc:
        logger.error("station %s: cannot read %s: %s", session.station_id, path, exc)
        writer.write(encode_file_begin(0, 0, safe_name))
        await writer.drain()
        session.fail_pending_download(f"{safe_name}: cannot read on server")
        return

    session.file_transfer_active = True
    session.file_transfer_name = safe_name
    started = loop.time()
    logger.info("station %s: sending %s (%d bytes, crc32=%08X)",
                session.station_id, safe_name, size, crc)

    try:
        # Held across FILE_BEGIN *and* the whole payload: the device reads the
        # payload by byte count, so anything written into the socket in between
        # (a CMD_REQUEST from the admin API) would end up inside its file.
        async with session.write_lock:
            writer.write(encode_file_begin(size, crc, safe_name))
            await writer.drain()

            # The file is read in the executor so a slow disk cannot stall the
            # event loop, and the size is fixed at CRC time — a file replaced
            # mid-transfer breaks the CRC on the device, which is the intended
            # outcome (it discards it, nothing half-new lands on the SD).
            sent = 0
            with open(path, "rb") as fh:
                while sent < size:
                    block = await loop.run_in_executor(None, fh.read, min(CHUNK, size - sent))
                    if not block:
                        break
                    writer.write(block)
                    await writer.drain()
                    sent += len(block)

        if sent != size:
            logger.error("station %s: %s truncated at %d/%d bytes",
                         session.station_id, safe_name, sent, size)
        else:
            logger.info("station %s: %s sent in %.1f s",
                        session.station_id, safe_name, loop.time() - started)
    except (ConnectionError, asyncio.CancelledError) as exc:
        # The device dropped out mid-transfer. Its .part file is discarded on its
        # side; nothing to clean up here.
        logger.warning("station %s: transfer of %s interrupted: %s",
                       session.station_id, safe_name, exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("station %s: transfer of %s failed: %s",
                     session.station_id, safe_name, exc)
    finally:
        session.file_transfer_active = False
        session.file_transfer_name = None


# ==========================================================================
# Upload direction (Stage 2): the device pushes one SD file to us.
#
# Deliberately narrow — single files on demand ("upload <name>" over the remote
# CLI), never the ready4upload/ batch: in streaming mode the server builds its
# own .ubx from the live stream, so a bulk upload would be duplicated work
# competing with the live data for the same uplink.
#
# The device announces total + CRC32 in FILE_UP_BEGIN, then sends framed
# FILE_UP_DATA chunks. We accept the file only if the byte count matches AND the
# CRC does — a partial upload is written to <name>.part and never published.
# ==========================================================================

class UploadReceiver:
    """Reassembles one incoming file for a station."""

    def __init__(self, root: Path, station_id: int, reply=None):
        # `reply(bytes)` sends a frame back to the device - here the FILE_UP_ACK
        # verdict. None (replay.py, tests) means nobody is listening.
        self._reply = reply
        # resolve() rather than str(station_id): the station's archive may be
        # under its label ("A001_2001"), and an upload belongs next to it, not
        # in a second bare-id directory. FileSink has already adopted the
        # directory by the time any upload arrives.
        self._dir = stationdir.resolve(root, station_id) / "uploads"
        self.station_id = station_id
        self._fh = None
        self._part: Path | None = None
        self._final: Path | None = None
        self.name: str | None = None
        self.total = 0
        self.received = 0
        self.crc_expected = 0
        self._crc = 0
        self._next_seq = 0

    @property
    def active(self) -> bool:
        return self._fh is not None

    def begin(self, msg: FileUpBegin) -> None:
        # No ACK here: it would arrive AFTER the new BEGIN and the device would
        # read it as the verdict on the new file.
        self.abort("superseded by a new upload", ack=False)   # no-op when idle

        safe = sanitize_filename(msg.name)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._dir.mkdir(parents=True, exist_ok=True)
        # Timestamped: an "upload the log" is a snapshot, and a second one must
        # not silently overwrite the first.
        self._final = self._dir / f"{Path(safe).stem}_{stamp}{Path(safe).suffix}"
        # Plain concatenation, not with_suffix(): a sanitised name may have no
        # suffix at all, and with_suffix() is picky about those.
        self._part = self._final.parent / (self._final.name + ".part")

        self.name = safe
        self.total = msg.total
        self.crc_expected = msg.crc32
        self.received = 0
        self._crc = 0
        self._next_seq = 0
        self._fh = open(self._part, "wb")
        logger.info("station %s: receiving %s (%d bytes, crc32=%08X)",
                    self.station_id, safe, msg.total, msg.crc32)

    def data(self, msg: FileUpData) -> None:
        if self._fh is None:
            logger.warning("station %s: FILE_UP_DATA without FILE_UP_BEGIN - ignored",
                           self.station_id)
            return
        if msg.seq != self._next_seq:
            # Frames ride a checksummed envelope, so a gap means a dropped frame,
            # not corruption. Either way the file would be wrong — stop now.
            self.abort(f"sequence gap: expected {self._next_seq}, got {msg.seq}")
            return

        self._next_seq += 1
        self._fh.write(msg.data)
        self._crc = zlib.crc32(msg.data, self._crc)
        self.received += len(msg.data)

        if self.received >= self.total:
            self._finish()

    def _finish(self) -> None:
        self._fh.close()
        self._fh = None
        crc = self._crc & 0xFFFFFFFF

        if self.received != self.total:
            logger.error("station %s: %s size mismatch (%d of %d bytes)",
                         self.station_id, self.name, self.received, self.total)
            self._part.unlink(missing_ok=True)
            self._ack(UP_ACK_SIZE_MISMATCH, crc)
            return
        if crc != self.crc_expected:
            logger.error("station %s: %s CRC mismatch (got %08X, want %08X) - discarded",
                         self.station_id, self.name, crc, self.crc_expected)
            self._part.unlink(missing_ok=True)
            self._ack(UP_ACK_CRC_MISMATCH, crc)
            return

        self._part.replace(self._final)      # atomic on the same filesystem
        logger.info("station %s: stored %s (%d bytes, CRC OK)",
                    self.station_id, self._final.name, self.received)
        # Only after the rename: the device deletes its copy on this ACK, so it
        # must not go out before the file is really in place.
        self._ack(UP_ACK_OK, crc)

    def _ack(self, result: int, crc: int) -> None:
        if self._reply is None:
            return
        try:
            self._reply(encode_file_up_ack(result, crc))
        except Exception:   # a dead socket must not take the frame loop down
            logger.warning("station %s: could not send FILE_UP_ACK", self.station_id)

    def abort(self, reason: str, ack: bool = True) -> None:
        if self._fh is None:
            return
        self._fh.close()
        self._fh = None
        logger.warning("station %s: upload of %s aborted after %d/%d bytes (%s)",
                       self.station_id, self.name, self.received, self.total, reason)
        if self._part is not None:
            self._part.unlink(missing_ok=True)
        if ack:
            self._ack(UP_ACK_ABORTED, self.crc_expected)
