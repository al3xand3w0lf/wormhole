"""NTRIP caster forwarder — RTCM3 out of the demuxer, live, to an NTRIP caster.

One instance of this class is one push connection to one caster's one mountpoint.
`server.py`'s `_make_sinks()` can attach more than one instance per station to push
the same RTCM3 to more than one caster at once — see `caster/` for a caster bundled
with this repo (Millipede, auto-provisioned with one mountpoint per configured
station), and `STREAM_CASTER_*` in `.env.example` for wiring a station's RTCM3 to
any NTRIP caster, bundled or external.

Wire protocol is NTRIP 1.0 SOURCE push (what most casters' legacy handshake still
speaks): `SOURCE <password> <mountpoint>\\r\\n`, caster answers `ICY 200 OK\\r\\n` on
success, then the connection is a raw RTCM3 byte pipe until closed.

Verified against Millipede's own source (pbeyssac/millipede-caster,
`caster/ntripsrv.c`, `check_password()`): for this legacy path the caster passes
`user = NULL`, and the check is `(!user || ...) && password matches` — i.e. the
`username` field in `source.auth`'s `MOUNTPOINT:username:password` is only consulted
for the NTRIP2/HTTP-POST+Basic-Auth path, not this one. Mountpoint + password alone is
correct here; no username needed on the wire.
"""

import asyncio
import logging

from .sinks import Sink

logger = logging.getLogger("streaming")

_SOURCE_AGENT = b"NTRIP wormhole/1.0"
_RECONNECT_DELAY = 5.0
_CONNECT_TIMEOUT = 10.0
_HANDSHAKE_TIMEOUT = 10.0

# Bounded so a stuck/unreachable caster cannot grow memory without limit — frames are
# dropped, not queued forever, once this fills. RTCM3 frames are small
# (typically <=250 B), so this is a few hundred KB at most.
_QUEUE_MAXSIZE = 2048


class NtripCasterSink(Sink):
    """Forwards one station's RTCM3 stream to its caster mountpoint.

    `on_rtcm3` only enqueues — the actual socket is owned by a background task, so a
    stalled or unreachable caster never blocks frame routing for this or any other
    station (same principle as "one bad connection must never kill the event loop").
    Reconnects with a fixed delay on any failure; every attempt does the SOURCE
    handshake again.
    """

    def __init__(self, host: str, port: int, mountpoint: str, password: str):
        self.host = host
        self.port = port
        self.mountpoint = mountpoint
        self._password = password
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._task: asyncio.Task | None = None

    def on_rtcm3(self, raw: bytes, stamp, sysclk: bool) -> None:
        try:
            if self._task is None:
                self._task = asyncio.get_running_loop().create_task(self._run())
            self._queue.put_nowait(raw)
        except asyncio.QueueFull:
            logger.debug("ntrip caster %s: queue full, dropping frame", self.mountpoint)
        except Exception as exc:  # noqa: BLE001 - must never propagate into route_frame
            logger.debug("ntrip caster %s: on_rtcm3 failed: %s", self.mountpoint, exc)

    async def _run(self) -> None:
        while True:
            try:
                await self._push_until_broken()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a caster outage must never kill the loop
                logger.debug("ntrip caster %s: %s", self.mountpoint, exc)
            await asyncio.sleep(_RECONNECT_DELAY)

    async def _push_until_broken(self) -> None:
        # A firewalled/unreachable host can otherwise hang the connect attempt far
        # longer than TCP's own retries would suggest — bound it explicitly so one
        # bad host cannot stall this station's reconnect loop indefinitely.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=_CONNECT_TIMEOUT
        )
        try:
            request = (
                b"SOURCE " + self._password.encode("ascii") + b" "
                + self.mountpoint.encode("ascii") + b"\r\n"
                + b"Source-Agent: " + _SOURCE_AGENT + b"\r\n"
                + b"\r\n"
            )
            writer.write(request)
            await writer.drain()

            response = await asyncio.wait_for(reader.readline(), timeout=_HANDSHAKE_TIMEOUT)
            if not response.upper().startswith(b"ICY 200"):
                logger.warning(
                    "ntrip caster %s: handshake rejected: %r", self.mountpoint, response
                )
                return

            logger.info("ntrip caster %s: connected to %s:%d", self.mountpoint, self.host, self.port)
            while True:
                data = await self._queue.get()
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - socket may already be dead
                pass

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
