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

# load .env
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# --- Configuration (via .env or defaults) ---
API_KEY = os.getenv("API_KEY", "changeme")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "data" / "incoming"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", BASE_DIR / "data" / "outgoing"))
LOG_FILE = os.getenv("LOG_FILE", str(BASE_DIR / "server.log"))
ACCESS_LOG_FILE = os.getenv("ACCESS_LOG_FILE", str(BASE_DIR / "server.access.log"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
SSL_CERTFILE = os.getenv("SSL_CERTFILE", str(BASE_DIR / "cert.pem"))
SSL_KEYFILE = os.getenv("SSL_KEYFILE", str(BASE_DIR / "key.pem"))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(50 * 1024 * 1024)))  # 50 MB
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

# FastAPI app
app = FastAPI(
    title="IoT File Server",
    description="HTTP(S) file server for IoT devices",
    version="1.1.0",
)

# API key auth
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    if api_key != API_KEY:
        logger.warning(f"Invalid API key: {api_key}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return api_key


def _sanitize_filename(filename: str) -> str:
    import re
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', filename)
    sanitized = sanitized.replace('..', '_').strip()
    if len(sanitized) > 255:
        sanitized = Path(sanitized).stem[:200] + Path(sanitized).suffix
    if not sanitized or sanitized in ('.', '..'):
        sanitized = f"unknown_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    return sanitized


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


# Upload: IoT device sends raw binary data
@app.post("/modem/upload",
          status_code=status.HTTP_201_CREATED,
          tags=["Upload"],
          dependencies=[Depends(verify_api_key)])
async def upload(
    request: Request,
    device_id: str = Query(..., description="Device ID"),
    filename: str = Query(..., description="Filename"),
):
    """
    Receives raw binary data in the POST body.

    Query parameters:
    - device_id: device identifier (e.g. 10000002)
    - filename: target filename (e.g. 10000002_260306_1000.ubx)
    """
    ext = Path(filename).suffix.lower()
    if ext in BLOCKED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File extension '{ext}' not allowed")

    safe_name = _sanitize_filename(filename)
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
