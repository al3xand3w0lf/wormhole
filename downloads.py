"""Server-side resolution of files a device may download.

Shared deliberately. The batch server (`server.py`, `/modem/download/{filename}`)
and the streaming server (`streaming/filetransfer.py`) hand out the *same* files
to the *same* devices — a device in streaming mode asks for `device.bin` over
the TCP stream, the identical unit in batch mode asks for it over HTTP. Two
copies of this logic would drift, and the drift would only surface when a
device is switched back to batch mode, long after the change.

So: one directory, one name sanitiser, one lookup.
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", BASE_DIR / "data" / "outgoing"))


def sanitize_filename(filename: str) -> str:
    """Strip path separators and traversal from a device-supplied name."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", filename)
    sanitized = sanitized.replace("..", "_").strip()
    if len(sanitized) > 255:
        sanitized = Path(sanitized).stem[:200] + Path(sanitized).suffix
    if not sanitized or sanitized in (".", ".."):
        sanitized = f"unknown_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    return sanitized


def resolve_download(filename: str) -> tuple[str, Path | None]:
    """Map a requested name onto a file in DOWNLOAD_DIR.

    Returns (safe_name, path) with path=None when the file does not exist. The
    caller decides how to report that — HTTP 404 on the batch side, a FILE_BEGIN
    with total=0 on the streaming side.
    """
    safe_name = sanitize_filename(filename)
    path = DOWNLOAD_DIR / safe_name
    if not path.is_file():
        return safe_name, None
    return safe_name, path
