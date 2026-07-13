"""TCP data plane (:9000) + HTTP admin/CLI control plane (:9001).

Both run in one asyncio event loop, in a process separate from the batch HTTP
server (server.py), which stays untouched.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from . import config
from .framer import StreamFramer
from .pipeline import ident_station_id, is_ident, route_frame
from .sinks import FileSink
from .station import StationRegistry, is_download_class

logger = logging.getLogger("streaming")


def setup_logging() -> None:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handlers = [
        RotatingFileHandler(
            config.LOG_FILE,
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT,
        ),
        logging.StreamHandler(),  # journald
    ]
    for handler in handlers:
        handler.setFormatter(fmt)
        logger.addHandler(handler)


def sweep_stale_raw() -> None:
    """Drop raw captures older than STREAM_RAW_MAX_AGE_H (they are the bulk of the data)."""
    if config.STREAM_RAW_MAX_AGE_H <= 0:
        return
    cutoff = time.time() - config.STREAM_RAW_MAX_AGE_H * 3600
    removed = 0
    for path in config.STREAM_DIR.glob("*/raw/*.bin"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("swept %d stale raw capture(s)", removed)


def _make_sinks(station_id: int) -> list:
    return [FileSink(config.STREAM_DIR, station_id, raw_capture=config.STREAM_RAW_CAPTURE)]


registry = StationRegistry(_make_sinks)


# ==========================================================================
# TCP data plane
# ==========================================================================
async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer_info = writer.get_extra_info("peername")
    peer = f"{peer_info[0]}:{peer_info[1]}" if peer_info else "?"
    logger.info("connection from %s", peer)

    framer = StreamFramer()
    session = None
    pending_raw = bytearray()  # bytes received before IDENT told us who this is
    prev_resync = prev_garbage = 0

    try:
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), config.STREAM_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("%s idle for %ds - closing", peer, config.STREAM_IDLE_TIMEOUT)
                break
            if not data:
                break

            chunk_accounted = False
            if session is None:
                pending_raw.extend(data)
                if len(pending_raw) > config.STREAM_PRE_IDENT_CAP:
                    logger.warning("%s sent %d bytes without IDENT - dropping", peer, len(pending_raw))
                    break

            for frame in framer.feed(data):
                if session is None:
                    if not is_ident(frame):
                        continue  # cannot attribute anything before IDENT
                    station_id = ident_station_id(frame)
                    if station_id is None:
                        continue
                    session = registry.get_or_create(station_id)
                    session.bind(writer, peer)
                    logger.info("station %s identified from %s", station_id, peer)

                    # Flush everything received so far (including this chunk).
                    session.bytes_rx += len(pending_raw)
                    for sink in session.sinks:
                        sink.on_raw(bytes(pending_raw))
                    pending_raw.clear()
                    chunk_accounted = True
                    continue

                route_frame(session, frame)

            if session is not None:
                if not chunk_accounted:
                    session.bytes_rx += len(data)
                    for sink in session.sinks:
                        sink.on_raw(data)
                session.resync_events += framer.resync_events - prev_resync
                session.garbage_bytes += framer.garbage_bytes - prev_garbage
            prev_resync, prev_garbage = framer.resync_events, framer.garbage_bytes

    except (ConnectionResetError, BrokenPipeError) as exc:
        logger.info("%s connection reset: %s", peer, exc)
    except Exception:  # noqa: BLE001 - one bad connection must not kill the server
        logger.exception("%s handler error", peer)
    finally:
        if session is not None:
            session.unbind(writer)
            logger.info("station %s disconnected (%s)", session.station_id, peer)
        else:
            logger.info("%s disconnected without IDENT", peer)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


# ==========================================================================
# HTTP admin / CLI control plane
# ==========================================================================
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    if api_key != config.API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return api_key


class CliRequest(BaseModel):
    cmd: str = Field(..., min_length=1, max_length=200)
    timeout: float | None = Field(None, gt=0, le=3600)


admin = FastAPI(title="Streaming Server", version="0.1.0")


@admin.get("/health", tags=["System"])
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stations": len(registry.all()),
        "connected": sum(1 for s in registry.all() if s.connected),
    }


@admin.get("/stream/stations", tags=["Stream"])
async def list_stations(_: str = Depends(verify_api_key)):
    return {"stations": [s.status() for s in registry.all()]}


@admin.post("/stream/{station_id}/cli", tags=["Stream"])
async def send_cli(station_id: int, req: CliRequest, _: str = Depends(verify_api_key)):
    session = registry.get(station_id)
    if session is None:
        raise HTTPException(404, f"unknown station {station_id}")
    if not session.connected:
        raise HTTPException(409, f"station {station_id} not connected")

    timeout = req.timeout
    if timeout is None:
        # download/downloadfw close the socket, transfer, then reconnect to answer.
        timeout = (
            config.STREAM_CLI_TRANSFER_TIMEOUT
            if is_download_class(req.cmd)
            else config.STREAM_CLI_TIMEOUT
        )

    try:
        text = await session.send_cli(req.cmd, config.STREAM_CLI_SECRET, timeout)
    except ConnectionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except asyncio.TimeoutError:
        raise HTTPException(504, f"no response within {timeout}s") from None

    return {"ok": True, "station_id": station_id, "cmd": req.cmd, "response": text}


# ==========================================================================
# Lifecycle
# ==========================================================================
async def run() -> None:
    setup_logging()
    config.STREAM_DIR.mkdir(parents=True, exist_ok=True)
    sweep_stale_raw()

    tcp = await asyncio.start_server(handle_connection, config.STREAM_HOST, config.STREAM_PORT)
    logger.info("TCP data plane on %s:%d", config.STREAM_HOST, config.STREAM_PORT)
    if not config.STREAM_CLI_SECRET:
        logger.warning("STREAM_CLI_SECRET is empty - remote CLI is unauthenticated")

    uv = uvicorn.Server(
        uvicorn.Config(
            admin,
            host=config.STREAM_HOST,
            port=config.STREAM_ADMIN_PORT,
            log_level="warning",
            access_log=False,
        )
    )
    logger.info("admin API on %s:%d", config.STREAM_HOST, config.STREAM_ADMIN_PORT)

    try:
        async with tcp:
            await asyncio.gather(tcp.serve_forever(), uv.serve())
    finally:
        registry.close_all()
