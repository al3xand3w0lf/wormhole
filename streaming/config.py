"""Configuration for the streaming server (.env, STREAM_* keys).

Mirrors the .env conventions of the batch server (server.py). The API_KEY is shared
so the admin API uses the same X-API-Key as the batch server.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Shared with the batch server
API_KEY = os.getenv("API_KEY", "changeme")

STREAM_HOST = os.getenv("STREAM_HOST", "0.0.0.0")
STREAM_PORT = int(os.getenv("STREAM_PORT", "9000"))
STREAM_ADMIN_PORT = int(os.getenv("STREAM_ADMIN_PORT", "9001"))
STREAM_DIR = Path(os.getenv("STREAM_DIR", BASE_DIR / "data" / "incoming_stream"))

# Must match `streaming_cli_secret` in the device's CONFIG.TXT. Empty = no auth
# (the device still expects the tok_len prefix, which we send as 0).
STREAM_CLI_SECRET = os.getenv("STREAM_CLI_SECRET", "")

STREAM_RAW_CAPTURE = _bool("STREAM_RAW_CAPTURE", True)
STREAM_RAW_MAX_AGE_H = int(os.getenv("STREAM_RAW_MAX_AGE_H", "168"))  # 7 days

# No bytes at all for this long -> drop the socket. Must exceed the device's
# heartbeat interval (default 30 s).
STREAM_IDLE_TIMEOUT = int(os.getenv("STREAM_IDLE_TIMEOUT", "180"))

STREAM_CLI_TIMEOUT = int(os.getenv("STREAM_CLI_TIMEOUT", "60"))
# download/downloadfw make the device close the socket, transfer, and reconnect.
STREAM_CLI_TRANSFER_TIMEOUT = int(os.getenv("STREAM_CLI_TRANSFER_TIMEOUT", "600"))

# A connection must identify itself (IDENT frame) within this many bytes.
STREAM_PRE_IDENT_CAP = int(os.getenv("STREAM_PRE_IDENT_CAP", "8192"))

LOG_FILE = os.getenv("STREAM_LOG_FILE", str(BASE_DIR / "streaming.log"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
