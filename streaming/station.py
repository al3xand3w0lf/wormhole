"""Per-station session state and the CLI request/response machinery.

A `StationSession` outlives its TCP connection. That is not an accident: a
`download` / `downloadfw` command makes the device close the streaming socket, run
the transfer over the same modem AT channel, then reconnect with a fresh IDENT and
only *then* send the buffered command output. If the pending CLI future lived on
the connection it would be destroyed by that disconnect and the answer would be
lost. Sessions are therefore keyed by station id in `StationRegistry`.
"""

import asyncio
import logging
from datetime import datetime, timezone

from .frames import CliResponse, encode_cmd_request
from .gpstime import GnssClock
from .sinks import Sink

logger = logging.getLogger("streaming")

# Commands that make the device pause the stream, transfer, and reconnect
# (any command whose name starts with "download", in the reference firmware).
_DOWNLOAD_PREFIX = "download"

# The ack the device sends over the still-open socket before it closes it.
_TRANSFER_ACK = "stream paused for transfer"


def is_download_class(cmd: str) -> bool:
    return cmd.strip().startswith(_DOWNLOAD_PREFIX)


class StationSession:
    """State for one station: clock, sinks, counters, connection, CLI queue."""

    def __init__(self, station_id: int, sinks: list[Sink]):
        self.station_id = station_id
        self.sinks = sinks
        self.clock = GnssClock()

        self.writer: asyncio.StreamWriter | None = None
        self.peer: str | None = None
        self.connected_since: datetime | None = None
        self.last_frame_at: datetime | None = None
        # Legacy path (streaming_file_transfer = 0): the device closes the socket
        # for an HTTP/FTP transfer and reconnects to answer.
        self.transfer_in_progress = False
        # Current path: the transfer rides this socket, so it has a name and a
        # task instead of a disconnect.
        self.file_transfer_active = False
        self.file_transfer_name: str | None = None
        self.file_transfer_task = None
        # Reassembles an incoming "upload <file>" (Stage 2); created on first use.
        self.upload_receiver = None

        self.bytes_rx = 0
        self.ubx_frames = 0
        self.rtcm3_frames = 0
        self.private_frames = 0
        self.resync_events = 0
        self.garbage_bytes = 0
        # RTCM3 message-number histogram — shows at a glance whether the base is
        # actually emitting 1005 + MSM7 + 1230.
        self.rtcm3_types: dict[int, int] = {}

        # Serialises everything written INTO the socket. A file transfer writes
        # raw, unframed bytes that the device consumes by byte count, so a
        # CMD_REQUEST frame slipped in between two payload writes would land
        # inside the file — the same kind of splice that can tear a multi-chunk
        # frame on the uplink, in the other direction.
        self.write_lock = asyncio.Lock()

        self._cli_lock = asyncio.Lock()
        self._cli_future: asyncio.Future | None = None
        self._cli_chunks: list[str] = []
        self._cli_awaiting_deferred = False
        self._cli_acked = False

    # -- connection binding -------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.writer is not None

    def bind(self, writer: asyncio.StreamWriter, peer: str) -> None:
        # A device that re-dials (new IP) often opens the new socket *before* the
        # old one is reaped, so the stale socket would linger until its idle
        # timeout. Drop it now: only the newest connection may own the station.
        old = self.writer
        if old is not None and old is not writer:
            logger.info("station %s superseded by %s - closing stale %s",
                        self.station_id, peer, self.peer)
            try:
                old.close()
            except Exception:  # noqa: BLE001 - the stale socket may already be dead
                pass

        self.writer = writer
        self.peer = peer
        self.connected_since = datetime.now(timezone.utc)
        if self.transfer_in_progress:
            logger.info(
                "station %s reconnected after transfer (awaiting deferred CLI output)",
                self.station_id,
            )
            self.transfer_in_progress = False

    def unbind(self, writer: asyncio.StreamWriter | None = None) -> None:
        """Release this connection's binding.

        `writer` identifies the caller's connection. A stale connection (already
        superseded by a newer one) must NOT clear the live binding — otherwise its
        delayed cleanup marks a happily streaming station as disconnected and kills
        the remote CLI. Pass None only where there is no competing connection (tests).
        """
        if writer is not None and self.writer is not writer:
            logger.info("station %s: stale connection closed, live one kept",
                        self.station_id)
            return

        self.writer = None
        self.peer = None
        # An upload in flight cannot survive the socket it was riding on.
        if self.upload_receiver is not None:
            self.upload_receiver.abort("connection closed")
        # A disconnect while a download-class command is outstanding is expected,
        # not an error: the device is transferring and will come back.
        if self._cli_awaiting_deferred and not self._done():
            self.transfer_in_progress = True
            logger.info(
                "station %s disconnected for transfer - keeping CLI request open",
                self.station_id,
            )

    # -- timestamps ---------------------------------------------------------

    def note_rtcm3_type(self, msg_type: int) -> None:
        if msg_type >= 0:
            self.rtcm3_types[msg_type] = self.rtcm3_types.get(msg_type, 0) + 1

    def stamp(self) -> tuple[datetime, bool]:
        """Current timestamp for file rotation: (datetime, using_system_clock)."""
        if self.clock.valid:
            return self.clock.gps_time, False
        return datetime.now(timezone.utc).replace(tzinfo=None), True

    # -- CLI ----------------------------------------------------------------

    def _done(self) -> bool:
        return self._cli_future is None or self._cli_future.done()

    async def send_cli(self, cmd: str, token: str, timeout: float) -> str:
        """Send a CLI command and await the reassembled response text."""
        async with self._cli_lock:
            if self.writer is None:
                raise ConnectionError("station not connected")

            # Encode first: the frame is ASCII-only, and a command that cannot be
            # encoded has to fail before the future exists. Raising past a created
            # future skips the finally below, leaving _done() False and cli_pending
            # stuck at true for the rest of the session.
            frame = encode_cmd_request(cmd, token)

            loop = asyncio.get_running_loop()
            self._cli_future = loop.create_future()
            self._cli_chunks = []
            self._cli_awaiting_deferred = is_download_class(cmd)
            self._cli_acked = False

            self._log_cli(f"->\t{cmd}")
            # Waits out a running file transfer rather than splicing into it.
            try:
                async with self.write_lock:
                    # Re-read the writer under the lock: the check above is not
                    # enough. Waiting for write_lock can take arbitrarily long -
                    # a file transfer owns it for its whole duration, and since
                    # rover.py a correction stream contends for it several times
                    # a second - and unbind() clears the writer to None in that
                    # window when the device drops. Live 2026-08-06: a whoami
                    # against station 1001 hit exactly this and returned a 500.
                    writer = self.writer
                    if writer is None:
                        raise ConnectionError("station disconnected before the command was sent")
                    writer.write(frame)
                    await writer.drain()
            except BaseException:
                # Raising past a created future would leave _done() False and
                # cli_pending stuck true for the rest of the session - the same
                # hazard the encode above is ordered to avoid.
                self._cli_future = None
                raise

            try:
                return await asyncio.wait_for(self._cli_future, timeout)
            finally:
                self._cli_future = None
                self._cli_awaiting_deferred = False
                self._cli_acked = False

    def handle_cli_response(self, resp: CliResponse) -> None:
        """Feed a CLI_RESPONSE frame into the pending request."""
        if self._done():
            # Unsolicited (e.g. the deferred answer arrived after we timed out).
            self._log_cli(f"<-\t{resp.text.strip()}")
            return

        self._cli_chunks.append(resp.text)
        if not resp.last:
            return

        # A download-class command answers twice: first the ack over the still-open
        # socket, then — after the transfer and reconnect — the real output. Only
        # the second one completes the request.
        if self._cli_awaiting_deferred and not self._cli_acked:
            joined = "".join(self._cli_chunks)
            if _TRANSFER_ACK in joined:
                self._cli_acked = True
                self._cli_chunks = []
                self._log_cli(f"<-\t{joined.strip()} (ack, awaiting transfer)")
                return

        text = "".join(self._cli_chunks)
        self._cli_chunks = []
        self._log_cli(f"<-\t{text.strip()}")
        if not self._cli_future.done():
            self._cli_future.set_result(text)

    def fail_pending_download(self, message: str) -> None:
        """Resolve a pending download-class CLI request early.

        For a transfer failure the server already knows the outcome (it answered
        FILE_BEGIN itself, or saw FILE_STATUS(ABORTED)) and has no reason to wait
        out the full timeout for a CLI_RESPONSE that a failed transfer may never
        produce.
        """
        if self._cli_awaiting_deferred and not self._done():
            self._log_cli(f"<-\t{message} (server-detected, no device response)")
            if not self._cli_future.done():
                self._cli_future.set_result(message)

    def _log_cli(self, line: str) -> None:
        stamp, sysclk = self.stamp()
        for sink in self.sinks:
            sink.on_cli(line, stamp, sysclk)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        for sink in self.sinks:
            sink.close()

    def status(self) -> dict:
        gps = self.clock.gps_time
        return {
            "station_id": self.station_id,
            "connected": self.connected,
            "peer": self.peer,
            "connected_since": self.connected_since.isoformat() if self.connected_since else None,
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "transfer_in_progress": self.transfer_in_progress,
            "file_transfer": self.file_transfer_name if self.file_transfer_active else None,
            "gps_time": gps.isoformat() if gps else None,
            "leap_s": self.clock.leap_s,
            "bytes_rx": self.bytes_rx,
            "frames": {
                "ubx": self.ubx_frames,
                "rtcm3": self.rtcm3_frames,
                "private": self.private_frames,
            },
            "rtcm3_types": dict(sorted(self.rtcm3_types.items())),
            "resync_events": self.resync_events,
            "garbage_bytes": self.garbage_bytes,
            "cli_pending": not self._done(),
        }


class StationRegistry:
    """Holds sessions by station id so they survive reconnects."""

    def __init__(self, sink_factory):
        self._sink_factory = sink_factory
        self._sessions: dict[int, StationSession] = {}

    def get_or_create(self, station_id: int) -> StationSession:
        session = self._sessions.get(station_id)
        if session is None:
            session = StationSession(station_id, self._sink_factory(station_id))
            self._sessions[station_id] = session
        return session

    def get(self, station_id: int) -> StationSession | None:
        return self._sessions.get(station_id)

    def all(self) -> list[StationSession]:
        return list(self._sessions.values())

    def close_all(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()
