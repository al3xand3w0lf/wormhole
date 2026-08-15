#!/usr/bin/env python3
"""
IoT File Server

HTTP(S) server for IoT devices.
Receives and serves files via raw HTTP POST/GET.

Usage:
    python server.py                    # Start with config from .env
    python server.py --port 8080        # Override port
    python server.py --no-ssl           # Disable SSL
"""

import os
import asyncio
import contextvars
import logging
import secrets
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import aiofiles

from fastapi import FastAPI, HTTPException, Depends, Request, Query, status
from fastapi.security import APIKeyHeader
from fastapi.responses import JSONResponse, FileResponse
import uvicorn
from dotenv import load_dotenv

from downloads import DOWNLOAD_DIR, sanitize_filename

# load .env
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# --- Configuration (via .env or defaults) ---
API_KEY = os.getenv("API_KEY", "changeme")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "data" / "incoming"))
# DOWNLOAD_DIR comes from downloads.py (same .env key) so the streaming server
# serves from the identical directory.
LOG_FILE = os.getenv("LOG_FILE", str(BASE_DIR / "server.log"))
ACCESS_LOG_FILE = os.getenv("ACCESS_LOG_FILE", str(BASE_DIR / "server.access.log"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
SSL_CERTFILE = os.getenv("SSL_CERTFILE", str(BASE_DIR / "cert.pem"))
SSL_KEYFILE = os.getenv("SSL_KEYFILE", str(BASE_DIR / "key.pem"))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(50 * 1024 * 1024)))  # 50 MB per single request/chunk
# Chunked uploads (device splits a file too large for its modem buffer into
# UFS-sized pieces that the server reassembles): the *reassembled* file may be
# larger than a single chunk, so it has its own ceiling.
MAX_ASSEMBLED_SIZE = int(os.getenv("MAX_ASSEMBLED_SIZE", str(500 * 1024 * 1024)))  # 500 MB
# Abandoned .partial files (a device that never sent its final chunk) are swept
# at startup once older than this.
PARTIAL_MAX_AGE_H = int(os.getenv("PARTIAL_MAX_AGE_H", "48"))  # hours
UPLOAD_CHUNK_TIMEOUT = int(os.getenv("UPLOAD_CHUNK_TIMEOUT", "30"))  # seconds per chunk read
WORKERS = int(os.getenv("WORKERS", "1"))

BLOCKED_EXTENSIONS = {".exe", ".bat", ".sh", ".cmd", ".scr", ".com", ".pif"}

# create directories
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --- Logging: per-request IDs + rotating files ---

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Injects the current request ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def _build_logger(name: str, filename: str, fmt: str) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.handlers.clear()
    formatter = logging.Formatter(fmt)
    rid_filter = RequestIdFilter()
    handlers = [
        RotatingFileHandler(filename, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT),
        logging.StreamHandler(),  # also to stdout for journald
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(rid_filter)
        lg.addHandler(handler)
    return lg


# server.log -> operational events (uploads/downloads, warnings, errors, lifecycle)
#               plus 4xx/5xx on real endpoints
logger = _build_logger(
    "iot-server", LOG_FILE,
    "%(asctime)s - %(levelname)s - [req:%(request_id)s] - %(message)s",
)
# server.access.log -> every HTTP request (one line each)
access_logger = _build_logger(
    "iot-access", ACCESS_LOG_FILE,
    "%(asctime)s - [req:%(request_id)s] - %(message)s",
)


def _sweep_stale_partials() -> None:
    """Remove abandoned <name>.partial reassembly files older than the max age.

    A fresh upload of the same file re-truncates its own .partial (chunk 0 opens
    'wb'), so this only reaps uploads a device gave up on entirely."""
    cutoff = time.time() - PARTIAL_MAX_AGE_H * 3600
    for p in UPLOAD_DIR.glob("*.partial"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                logger.info(f"Removed stale partial: {p.name}")
        except OSError:
            pass


_sweep_stale_partials()

# FastAPI app
app = FastAPI(
    title="IoT File Server",
    description="HTTP(S) file server for IoT devices",
    version="1.2.0",
)

# API key auth
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    if api_key != API_KEY:
        logger.warning(f"Invalid API key: {api_key}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return api_key


# Shared with the streaming server so both hand out the same files under the
# same names — see downloads.py.
_sanitize_filename = sanitize_filename


# --- Middleware ---

# Real application endpoints — anything else is treated as scanner/noise traffic
def _is_real_endpoint(path: str) -> bool:
    return path == "/health" or path == "/uploads" or path.startswith("/modem/")


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = secrets.token_hex(4)
    token = request_id_ctx.set(rid)
    start = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        duration = time.perf_counter() - start
        client = request.client.host if request.client else "-"
        status_code = response.status_code if response is not None else 500
        line = f"{request.method} {request.url.path} from {client} -> {status_code} ({duration:.3f}s)"
        # Every request → access log
        access_logger.info(line)
        # Operational log: only real endpoints with errors, plus all 5xx —
        # keeps server.log free of internet-scanner noise
        if status_code >= 500 or (status_code >= 400 and _is_real_endpoint(request.url.path)):
            logger.warning(line)
        if response is not None:
            response.headers["X-Request-ID"] = rid
        request_id_ctx.reset(token)


# --- Endpoints ---

@app.get("/health", tags=["System"])
async def health():
    """Health check (no API key required)."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


async def _handle_chunk_upload(request, device_id, safe_name,
                               chunk_index, chunk_count, offset, total_size):
    """Reassemble a chunked upload by writing each chunk at its byte offset.

    Contract (matches the device firmware): a non-final chunk is answered 202;
    the final chunk (chunk_index == chunk_count - 1) is verified against
    total_size and the .partial file is atomically renamed to the target name
    (201). Chunk 0 truncates the .partial, so a full retry is idempotent; other
    chunks seek to `offset`, so re-sending the same chunk is idempotent too.
    """
    if offset is None or total_size is None:
        raise HTTPException(status_code=400, detail="chunked upload requires offset and total_size")
    if chunk_index < 0 or chunk_index >= chunk_count or offset < 0 or total_size <= 0:
        raise HTTPException(status_code=400, detail="invalid chunk parameters")
    if total_size > MAX_ASSEMBLED_SIZE:
        raise HTTPException(status_code=413,
                            detail=f"assembled file too large ({total_size} > {MAX_ASSEMBLED_SIZE})")

    is_final = (chunk_index == chunk_count - 1)
    # Device-scoped partial name so two devices can never clash on the same target.
    partial_path = UPLOAD_DIR / f"{safe_name}.{_sanitize_filename(device_id)}.partial"

    if chunk_index == 0:
        mode = "wb"          # first chunk creates/truncates
    else:
        if not partial_path.exists():
            # Lost partial (server restart / swept) — device must restart from chunk 0.
            logger.warning(f"Chunk {chunk_index} for {safe_name} but no .partial — request restart")
            raise HTTPException(status_code=409, detail="partial missing, restart from chunk 0")
        mode = "r+b"         # patch existing partial at offset

    content_length = int(request.headers.get("content-length", 0))
    written = 0
    try:
        async with aiofiles.open(partial_path, mode) as f:
            await f.seek(offset)
            stream = request.stream().__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(stream.__anext__(), timeout=UPLOAD_CHUNK_TIMEOUT)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise HTTPException(status_code=408, detail="Upload timed out")
                written += len(chunk)
                if written > MAX_FILE_SIZE:
                    raise HTTPException(status_code=413, detail="Chunk too large")
                if offset + written > total_size:
                    raise HTTPException(status_code=400, detail="chunk exceeds total_size")
                await f.write(chunk)
                if content_length and written >= content_length:
                    break
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Chunk upload error ({device_id}, {safe_name} #{chunk_index}): {e}")
        raise HTTPException(status_code=500, detail="Server error")

    if not is_final:
        logger.info(f"Chunk {chunk_index + 1}/{chunk_count}: {safe_name} "
                    f"(+{written:,} @ {offset:,}) from {device_id}")
        return JSONResponse(status_code=202, content={
            "status": "chunk_accepted",
            "filename": safe_name,
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "offset": offset,
            "received": written,
        })

    # Final chunk: verify the assembled size, then finalise.
    actual = partial_path.stat().st_size
    if actual != total_size:
        logger.warning(f"Reassembled size mismatch for {safe_name}: "
                       f"got {actual}, expected {total_size} (partial kept)")
        raise HTTPException(status_code=422,
                            detail=f"reassembled size {actual} != total_size {total_size}")

    final_name = safe_name
    final_path = UPLOAD_DIR / final_name
    if final_path.exists():
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        ts = datetime.now(timezone.utc).strftime('%H%M%S_%f')[:-3]
        final_name = f"{stem}_{ts}{suffix}"
        final_path = UPLOAD_DIR / final_name
        logger.warning(f"Collision on finalize, renamed to: {final_name}")

    partial_path.replace(final_path)  # atomic on the same filesystem
    logger.info(f"Reassembled OK: {final_name} ({actual:,} bytes, {chunk_count} chunks) from {device_id}")
    return JSONResponse(status_code=201, content={
        "status": "ok",
        "filename": final_name,
        "size": actual,
        "device_id": device_id,
        "chunks": chunk_count,
    })


# Upload: IoT device sends raw binary data
@app.post("/modem/upload",
          status_code=status.HTTP_201_CREATED,
          tags=["Upload"],
          dependencies=[Depends(verify_api_key)])
async def upload(
    request: Request,
    device_id: str = Query(..., description="Device ID"),
    filename: str = Query(..., description="Filename"),
    chunk_index: Optional[int] = Query(None, description="0-based chunk index (chunked upload)"),
    chunk_count: Optional[int] = Query(None, description="Total number of chunks"),
    offset: Optional[int] = Query(None, description="Byte offset of this chunk in the assembled file"),
    total_size: Optional[int] = Query(None, description="Full assembled file size in bytes"),
):
    """
    Receives raw binary data in the POST body.

    Query parameters:
    - device_id: device identifier (e.g. 10000002)
    - filename: target filename (e.g. 10000002_260306_1000.ubx)

    Optional chunked upload (a file too large for the device's modem buffer):
    - chunk_index / chunk_count / offset / total_size — each chunk's raw bytes are
      written at `offset` into `<filename>.<device>.partial`. A non-final chunk is
      answered 202 (Accepted); the final chunk is verified against total_size and
      the partial is atomically renamed to the target name (201). Requests without
      these params keep the original whole-file behaviour.
    """
    ext = Path(filename).suffix.lower()
    if ext in BLOCKED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File extension '{ext}' not allowed")

    safe_name = _sanitize_filename(filename)

    # Chunked upload path (device split a file larger than its modem buffer).
    if chunk_index is not None and chunk_count is not None and chunk_count > 1:
        return await _handle_chunk_upload(
            request, device_id, safe_name, chunk_index, chunk_count, offset, total_size
        )

    file_path = UPLOAD_DIR / safe_name

    # avoid collision
    if file_path.exists():
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        ts = datetime.now(timezone.utc).strftime('%H%M%S_%f')[:-3]
        safe_name = f"{stem}_{ts}{suffix}"
        file_path = UPLOAD_DIR / safe_name
        logger.warning(f"Collision, renamed to: {safe_name}")

    # Use Content-Length to know when body is complete — some IoT devices
    # do not send a clean EOF and stop after Content-Length bytes
    content_length = int(request.headers.get("content-length", 0))

    try:
        total = 0
        stream = request.stream().__aiter__()
        async with aiofiles.open(file_path, "wb") as f:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        stream.__anext__(), timeout=UPLOAD_CHUNK_TIMEOUT
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    # Modem froze mid-transfer — drop the partial file instead of
                    # blocking the coroutine until the OS TCP timeout (minutes)
                    file_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=408, detail="Upload timed out")
                total += len(chunk)
                if total > MAX_FILE_SIZE:
                    file_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="File too large")
                await f.write(chunk)
                if content_length and total >= content_length:
                    break  # All declared bytes received, respond immediately

        if total == 0:
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Empty request body")

        logger.info(f"Upload OK: {safe_name} ({total:,} bytes) from {device_id}")
        return {
            "status": "ok",
            "filename": safe_name,
            "size": total,
            "device_id": device_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload error ({device_id}): {e}")
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Server error")


# Download: IoT device fetches a file
@app.get("/modem/download/{filename}",
         tags=["Download"],
         dependencies=[Depends(verify_api_key)])
async def download(filename: str):
    """Serves a file as application/octet-stream."""
    safe_name = _sanitize_filename(filename)
    file_path = DOWNLOAD_DIR / safe_name

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"'{filename}' not found")

    logger.info(f"Download: {safe_name} ({file_path.stat().st_size:,} bytes)")
    return FileResponse(path=str(file_path), filename=safe_name, media_type="application/octet-stream")


# List downloads
@app.get("/modem/download",
         tags=["Download"],
         dependencies=[Depends(verify_api_key)])
async def list_downloads(device_id: Optional[str] = None):
    """Lists available files in the download directory."""
    pattern = f"{device_id}_*" if device_id else "*"
    files = []
    for fp in sorted(DOWNLOAD_DIR.glob(pattern)):
        if fp.is_file():
            files.append({"filename": fp.name, "size": fp.stat().st_size})
    return {"files": files, "count": len(files)}


# List uploads
@app.get("/uploads",
         tags=["Upload"],
         dependencies=[Depends(verify_api_key)])
async def list_uploads(device_id: Optional[str] = None, limit: int = 100):
    """Lists received files in the upload directory."""
    pattern = f"{device_id}_*" if device_id else "*"
    files = []
    for fp in UPLOAD_DIR.glob(pattern):
        if fp.is_file():
            stat = fp.stat()
            files.append({
                "filename": fp.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return {"files": files[:limit], "count": len(files)}


# --- Start server ---

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="IoT File Server")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-ssl", action="store_true", help="Disable SSL")
    args = parser.parse_args()

    kwargs = {
        # String import path required when workers > 1 (uvicorn multiprocessing)
        "app": "server:app" if WORKERS > 1 else app,
        "host": args.host,
        "port": args.port,
        "log_config": None,
        "workers": WORKERS if WORKERS > 1 else None,
    }

    cert = Path(SSL_CERTFILE)
    key = Path(SSL_KEYFILE)
    if not args.no_ssl and cert.exists() and key.exists():
        kwargs["ssl_certfile"] = str(cert)
        kwargs["ssl_keyfile"] = str(key)
        logger.info(f"HTTPS server starting on {args.host}:{args.port}")
    else:
        logger.info(f"HTTP server starting on {args.host}:{args.port}")

    logger.info(f"Upload dir:   {UPLOAD_DIR}")
    logger.info(f"Download dir: {DOWNLOAD_DIR}")

    uvicorn.run(**kwargs)
