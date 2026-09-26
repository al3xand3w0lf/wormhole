"""Configuration for the streaming server (.env, STREAM_* keys).

Mirrors the .env conventions of the batch server (server.py). The API_KEY is shared
so the admin API uses the same X-API-Key as the batch server.
"""

import os
from dataclasses import dataclass
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

# The address a DEVICE should dial to reach this installation.
#
# This cannot be derived from anything else here. HOST and STREAM_HOST are BIND
# addresses — almost always 0.0.0.0 — and a bind address says which interfaces
# to listen on, never what a station out in the field should be pointed at.
# Behind NAT, a reverse proxy or a dynamic DNS name the two have nothing to do
# with one another.
#
# Only the config generator reads it, to prefill the server address in a
# device's configuration file. Left empty it prefills nothing and says why,
# which is the right failure mode: a guessed address looks answered, and sends
# a whole fleet somewhere wrong.
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "").strip()

# The batch server's port, read from the same key server.py reads, so the
# generator has no second source of truth for it.
BATCH_PORT = int(os.getenv("PORT", "8000"))

# Must match `streaming_cli_secret` in the device's CONFIG.TXT. Empty = no auth
# (the device still expects the tok_len prefix, which we send as 0).
STREAM_CLI_SECRET = os.getenv("STREAM_CLI_SECRET", "")

STREAM_RAW_CAPTURE = _bool("STREAM_RAW_CAPTURE", True)
STREAM_RAW_MAX_AGE_H = int(os.getenv("STREAM_RAW_MAX_AGE_H", "168"))  # 7 days

# No bytes at all for this long -> drop the socket. Must exceed the device's
# heartbeat interval (default 30 s).
STREAM_IDLE_TIMEOUT = int(os.getenv("STREAM_IDLE_TIMEOUT", "180"))

STREAM_CLI_TIMEOUT = int(os.getenv("STREAM_CLI_TIMEOUT", "60"))
# download/downloadfw answer only once the file has crossed the stream.
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
# auto-subscription never
# touches a station id that appears here, or in STREAM_ROVER_STATIONS - a
# hand-configured pair always wins. Also hot-reloadable: POST
# /stream/rover/reload or SIGHUP re-reads this key and diffs it against the
# live routers, add/remove only, no restart (server.py's _apply_rover_bases()).
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
# A station that
# identifies (IDENT role byte) as ROLE_ROVER is subscribed to its nearest base
# automatically, live, no restart. A station identifying as ROLE_BASE only
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

# The external correction source, an NTRIP caster - used when there is no base in
# the fleet, or in addition to one. NTRIP clients need no registration there; the
# username is any valid email and the password is not checked.
STREAM_NTRIP_HOST = os.getenv("STREAM_NTRIP_HOST", "rtk2go.com")
STREAM_NTRIP_PORT = int(os.getenv("STREAM_NTRIP_PORT", "2101"))
STREAM_NTRIP_MOUNT = os.getenv("STREAM_NTRIP_MOUNT", "")
STREAM_NTRIP_USER = os.getenv("STREAM_NTRIP_USER", "")
STREAM_NTRIP_PASS = os.getenv("STREAM_NTRIP_PASS", "none")

# ---- Bundled NTRIP caster push (streaming/ntrip.py, caster/) --------------
# Forwards a station's demuxed RTCM3 live to an NTRIP caster mountpoint (one
# mountpoint per station, mountpoint name == station id) via NtripCasterSink.
# Distinct from STREAM_NTRIP_* above, which is the opposite direction (this
# server *pulling* corrections for a rover from an external caster).
#
# Defaults point at the caster bundled in caster/ (127.0.0.1, Millipede's
# default port) - see caster/README.md to build and provision it. Any NTRIP
# caster speaking the NTRIP 1.0 SOURCE handshake works here, bundled or not;
# only the host/port need to change to push elsewhere instead.
#
# A station missing from STREAM_CASTER_PASSWORDS is simply not forwarded -
# its mountpoint may not be provisioned. Empty (the default) turns this off
# entirely: no caster connection, no task, nothing to go wrong on a server
# that runs no caster.
STREAM_CASTER_ENABLE = _bool("STREAM_CASTER_ENABLE", False)
STREAM_CASTER_HOST = os.getenv("STREAM_CASTER_HOST", "127.0.0.1")
STREAM_CASTER_PORT = int(os.getenv("STREAM_CASTER_PORT", "2101"))


def _station_passwords(value: str) -> dict[int, str]:
    """Parse "1001:pw1,1002:pw2" into {1001: "pw1", 1002: "pw2"}."""
    result: dict[int, str] = {}
    for pair in value.replace(";", ",").split(","):
        station, _, password = pair.partition(":")
        station, password = station.strip(), password.strip()
        if station.isdigit() and password:
            result[int(station)] = password
    return result


STREAM_CASTER_PASSWORDS = _station_passwords(os.getenv("STREAM_CASTER_PASSWORDS", ""))

# The stations that have (or should have) a mountpoint on the bundled caster.
# caster/generate_config.py provisions from this list at setup time; the server
# reads it too, because the auto-provisioner below has to render the *complete*
# sourcetable, not just the station in front of it.
_caster_stations = os.getenv("STREAM_CASTER_STATIONS", "").replace(";", ",")
STREAM_CASTER_STATIONS = {int(s) for s in (p.strip() for p in _caster_stations.split(","))
                          if s.isdigit()}

# Automatic mountpoint provisioning: a station that identifies with role=base
# (IDENT) gets a mountpoint on the *bundled* caster the moment it connects -
# password generated, sourcetable/source.auth rewritten, caster SIGHUPed, sink
# attached live. No .env edit, no restart, no setup.sh re-run.
#
# The role byte alone is the gate, deliberately - see caster_provision.py's
# module docstring for why that is the right trade here and the wrong one in
# rover_discovery.py, which gates the same claim much harder. Off by default:
# it writes files (caster/millipede-caster/etc/, .env) and publishes a station
# under its own name, which is a decision a deployment should make on purpose.
#
# Only ever touches the bundled caster. STREAM_CASTER_TARGETS below are other
# people's casters whose config this server does not own - a station is
# provisioned there by hand, as before.
STREAM_CASTER_AUTO_ENABLE = _bool("STREAM_CASTER_AUTO_ENABLE", False)

# Where the bundled caster's config lives. Only the auto-provisioner writes
# here; everything else reaches the caster over the network and does not care
# where its files are. Defaults to the in-repo build (caster/setup.sh) - set it
# when the caster was installed elsewhere, or to point a second instance on this
# host at its own caster.
_caster_etc = os.getenv("STREAM_CASTER_ETC_DIR", "")
STREAM_CASTER_ETC_DIR = Path(_caster_etc) if _caster_etc else (
    BASE_DIR / "caster" / "millipede-caster" / "etc")


# ---- Additional NTRIP caster targets --------------------------------------
# The keys above configure one caster - the bundled one, whose credentials
# caster/setup.sh writes there itself. A deployment that pushes the same
# stations to *further* casters (a public one, a partner's, a second instance
# for evaluation) names them in STREAM_CASTER_TARGETS and gets one
# NtripCasterSink per target per station, all additive:
#
#   STREAM_CASTER_TARGETS=partner,bkg
#   STREAM_CASTER_PARTNER_HOST=ntrip.example.org
#   STREAM_CASTER_PARTNER_PORT=2101
#   STREAM_CASTER_PARTNER_PASSWORDS=1001:pw
#   STREAM_CASTER_BKG_PORT=2104
#   STREAM_CASTER_BKG_PASSWORDS=1001:shared,1002:shared
#
# The name is free-form; it only builds the key prefix (upper-cased, "-" -> "_")
# and labels the target in the log. Listing a name *is* the enable - a target
# is switched off by removing it from the list, or, to keep its settings around,
# by STREAM_CASTER_<NAME>_ENABLE=false.
#
# Per target, a station missing from that target's _PASSWORDS is not forwarded
# there: mountpoints are provisioned per caster, and a station may exist on one
# and not the next. Some casters (BKG's NTRIP 1.0 source auth, for one) use a
# single global password for every mountpoint - then every station in that
# target's list simply repeats the same value.


@dataclass(frozen=True)
class CasterTarget:
    """One additional caster to push to, from STREAM_CASTER_<NAME>_* keys."""

    name: str
    host: str
    port: int
    passwords: dict[int, str]


def _caster_targets(value: str) -> tuple[CasterTarget, ...]:
    targets = []
    for name in (n.strip() for n in value.replace(";", ",").split(",")):
        if not name:
            continue
        key = name.upper().replace("-", "_")
        if not _bool(f"STREAM_CASTER_{key}_ENABLE", True):
            continue
        targets.append(CasterTarget(
            name=name,
            host=os.getenv(f"STREAM_CASTER_{key}_HOST", "127.0.0.1"),
            port=int(os.getenv(f"STREAM_CASTER_{key}_PORT", "2101")),
            passwords=_station_passwords(os.getenv(f"STREAM_CASTER_{key}_PASSWORDS", "")),
        ))
    return tuple(targets)


STREAM_CASTER_TARGETS = _caster_targets(os.getenv("STREAM_CASTER_TARGETS", ""))

LOG_FILE = os.getenv("STREAM_LOG_FILE", str(BASE_DIR / "streaming.log"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
