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

# The admin API is not the data plane. STREAM_HOST has to be reachable for the
# device to dial STREAM_PORT; the admin API must not be, so it binds separately and
# to loopback by default. Sharing STREAM_HOST would put /stream/{id}/cli — reboot,
# downloadfw — on every interface, leaving a firewall as the only thing in the way.
STREAM_ADMIN_HOST = os.getenv("STREAM_ADMIN_HOST", "127.0.0.1")
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

# ---- RTK rover correction downlink (streaming/rover.py) --------------------
# Which stations receive corrections. Empty = the downlink is off entirely, so a
# server that is not running rovers pays nothing and opens no caster connection.
_rovers = os.getenv("STREAM_ROVER_STATIONS", "").replace(";", ",")
STREAM_ROVER_STATIONS = {int(s) for s in (p.strip() for p in _rovers.split(",")) if s.isdigit()}

# Per-rover queue depth, in whole RTCM3 frames. One epoch from a typical base is
# about six frames, so the default holds ~4 epochs. Deliberately shallow:
# corrections are epoch-bound and a deep queue only delivers staler data - it
# does not deliver more useful data. Freshness beats completeness.
STREAM_ROVER_QUEUE = int(os.getenv("STREAM_ROVER_QUEUE", "24"))

# Allowlist of RTCM3 message types forwarded to rovers. Empty (the default)
# forwards everything, which is what a link with headroom wants. It is a lever
# for a constrained one: the device drains its modem buffer at a fixed rate, so a
# type the receiver cannot use does not cost nothing - it costs a correction the
# receiver could have used. See streaming/rover.py's module docstring.
_rtypes = os.getenv("STREAM_ROVER_RTCM_TYPES", "").replace(";", ",")
STREAM_ROVER_RTCM_TYPES = {int(s) for s in (p.strip() for p in _rtypes.split(",")) if s.isdigit()}

# In-fleet correction routing: which base station feeds which rovers, entirely
# inside this server (a RoverSourceSink on the base's stream publishes into a
# RoverRouter, no external caster in the path). Format:
#   "base:rover[+rover...][,base:rover...]"   e.g. "1001:1002" or "1001:1002+1003,1005:1006"
# Each base gets its own router. This is a *manual pin*: rover_discovery.py's
# auto-subscription never touches a station id that appears here, or in
# STREAM_ROVER_STATIONS - a hand-configured pair always wins. Also
# hot-reloadable: POST /stream/rover/reload or SIGHUP re-reads this key and
# diffs it against the live routers, add/remove only, no restart
# (server.py's _apply_rover_bases()).
def parse_rover_bases(value: str) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {}
    for pair in value.replace(";", ",").split(","):
        if ":" not in pair:
            continue
        base_s, rovers_s = pair.split(":", 1)
        if not base_s.strip().isdigit():
            continue
        rovers = {int(r) for r in rovers_s.replace("+", " ").split() if r.isdigit()}
        if rovers:
            out.setdefault(int(base_s.strip()), set()).update(rovers)
    return out

STREAM_ROVER_BASES = parse_rover_bases(os.getenv("STREAM_ROVER_BASES", ""))

# ---- Automatic rover -> nearest-base subscription (rover_discovery.py) -----
# A station that identifies (IDENT role byte) as ROLE_ROVER is subscribed to
# its nearest base automatically, live, no restart. A station identifying as
# ROLE_BASE only
# becomes a correction source if its id is ALSO here or already a
# STREAM_ROVER_BASES key - role alone is not trust, see rover_discovery.py's
# module docstring for why.
STREAM_ROVER_AUTO_ENABLE = _bool("STREAM_ROVER_AUTO_ENABLE", True)

_auto_bases = os.getenv("STREAM_ROVER_AUTO_BASE_STATIONS", "").replace(";", ",")
STREAM_ROVER_AUTO_BASE_STATIONS = {int(s) for s in (p.strip() for p in _auto_bases.split(","))
                                    if s.isdigit()}

# Beyond this, a correction cannot help - do not subscribe, log why instead.
STREAM_ROVER_MAX_BASELINE_KM = float(os.getenv("STREAM_ROVER_MAX_BASELINE_KM", "40"))
# A rover only switches to a nearer base if it is nearer by more than this -
# hysteresis against flapping between two bases near their midpoint.
STREAM_ROVER_SWITCH_MARGIN_KM = float(os.getenv("STREAM_ROVER_SWITCH_MARGIN_KM", "5"))

# The external correction source, an NTRIP caster - used when there is no base
# in the fleet, or in addition to one. RTK2GO (rtk2go.com) is a good free
# public caster to test against: it needs no registration for clients, the
# username can be any valid email address and the password is not checked.
STREAM_NTRIP_HOST = os.getenv("STREAM_NTRIP_HOST", "rtk2go.com")
STREAM_NTRIP_PORT = int(os.getenv("STREAM_NTRIP_PORT", "2101"))
STREAM_NTRIP_MOUNT = os.getenv("STREAM_NTRIP_MOUNT", "")
STREAM_NTRIP_USER = os.getenv("STREAM_NTRIP_USER", "")
STREAM_NTRIP_PASS = os.getenv("STREAM_NTRIP_PASS", "none")

LOG_FILE = os.getenv("STREAM_LOG_FILE", str(BASE_DIR / "streaming.log"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
