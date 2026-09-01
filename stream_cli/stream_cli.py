#!/usr/bin/env python3
"""
stream_cli.py — interactive terminal for the streaming server's remote CLI.

A thin front-end over the admin API (:9001). It does NOT talk to the device
directly and contains no frame logic: it POSTs a command to
`/stream/{station_id}/cli` and prints the reassembled response. The server owns
everything hard — the CLI secret, waiting for the device's response frames,
reassembly, and the download/downloadfw disconnect-reconnect dance.

    POST /stream/{id}/cli  {"cmd": "..."}  ->  {"response": "..."}

Python standard library only. On the server host it needs NO configuration: it
reads API_KEY and the admin port straight from the repo `.env` (the same file the
servers use), so it just works:

    python stream_cli/stream_cli.py                   # REPL, auto-pick the station
    python stream_cli/stream_cli.py --station 1001    # REPL against station 1001
    python stream_cli/stream_cli.py sysinfo           # one-shot: send, print, exit
    python stream_cli/stream_cli.py --station 1001 whoami
    python stream_cli/stream_cli.py --list            # just list connected stations
    python stream_cli/stream_cli.py --list-all        # ... plus the remembered, offline ones

Config resolution (first hit wins), for both the API key and the admin URL:
    1. --api-key / --url on the command line
    2. $API_KEY / $STREAM_ADMIN_URL in the environment
    3. the repo .env (API_KEY, STREAM_ADMIN_PORT) — the zero-setup path on the host
    4. the CONFIG defaults below (for copying onto a machine without the .env)

REPL meta-commands (leading /):
    /stations        re-list connected stations ("/stations all" adds the offline ones)
    /station <id>    switch the active station
    /help            show device allowlist + meta-commands
    /quit, /exit     leave (Ctrl-D works too)
"""

# ═══════════════════════════════════════════════════════════════════════
#  CONFIG — last-resort defaults; only used when .env / env / flags are absent
#  (e.g. when this file is copied onto a host that has no repo .env)
# ═══════════════════════════════════════════════════════════════════════
ADMIN_URL = "http://127.0.0.1:9001"   # http(s)://<ip-or-host>:<admin-port>
API_KEY   = "changeme"                 # API key configured on the server (shared with batch)
VERIFY_TLS = False                     # ignored for http; set True to verify a real HTTPS cert
# ═══════════════════════════════════════════════════════════════════════

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


def load_env_file() -> dict:
    """Best-effort parse of the repo .env (KEY=VALUE lines) — the same file the
    servers load via python-dotenv. Located relative to this file (repo root is the
    parent of stream_cli/), with the cwd as a fallback. Stdlib only, no dependency;
    quietly returns {} if there is no .env (the copied-elsewhere case)."""
    for path in (Path(__file__).resolve().parent.parent / ".env", Path.cwd() / ".env"):
        if not path.is_file():
            continue
        env: dict = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env.setdefault(key.strip(), value.strip())
        return env
    return {}

# The device-side positive allowlist (mirrors ALLOWLIST in fake_device.py and the
# firmware). Shown in /help; not enforced here — the device rejects the rest.
ALLOWLIST = ("whoami", "sysinfo", "listfiles", "download", "downloadcf", "downloadfw",
             "upload", "reboot", "fsdcard")

# download/downloadfw pause the stream, transfer over the modem, then reconnect to
# answer — the server waits up to STREAM_CLI_TRANSFER_TIMEOUT (default 600 s) for
# that. Our HTTP read must outlast the server's wait, or we'd give up first.
NORMAL_HTTP_TIMEOUT = 70.0
TRANSFER_HTTP_TIMEOUT = 620.0


def is_download_class(cmd: str) -> bool:
    return cmd.strip().startswith("download")


def ssl_context() -> Optional[ssl.SSLContext]:
    """Skip certificate verification when VERIFY_TLS is False (self-signed certs)."""
    if VERIFY_TLS:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(path: str, ctx, timeout: float) -> dict:
    req = urllib.request.Request(
        f"{ADMIN_URL.rstrip('/')}{path}", headers={"X-API-Key": API_KEY}
    )
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _post(path: str, body: dict, ctx, timeout: float) -> dict:
    req = urllib.request.Request(
        f"{ADMIN_URL.rstrip('/')}{path}",
        data=json.dumps(body).encode(),
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_stations(ctx) -> list[dict]:
    return _get("/stream/stations", ctx, NORMAL_HTTP_TIMEOUT).get("stations", [])


def print_stations(stations: list[dict], show_all: bool = False) -> None:
    """Print the station table. By default only the *connected* stations — those
    are the ones a command can actually be sent to; a disconnected entry is a
    station the server still remembers (sessions are keyed by station id and
    outlive the connection), and listing it next to a live one only invites
    sending a command into the void. `show_all` prints the remembered ones too,
    connected first, with the `connected` column that then carries information."""
    if not stations:
        print("(no stations known to the server yet)")
        return

    connected = [s for s in stations if s.get("connected")]
    offline = [s for s in stations if not s.get("connected")]
    rows = connected + offline if show_all else connected

    if not rows:
        print(f"(no station connected; {len(offline)} known to the server — "
              f"use --list-all / '/stations all' to see them)")
        return

    if show_all:
        print(f"{'station':>8}  {'connected':>9}  {'gps_time':<27}  {'pending':>7}  peer")
    else:
        print(f"{'station':>8}  {'gps_time':<27}  {'pending':>7}  peer")
    for s in rows:
        connected_col = f"{str(s['connected']):>9}  " if show_all else ""
        print(
            f"{s['station_id']:>8}  {connected_col}"
            f"{str(s.get('gps_time')):<27}  {str(s.get('cli_pending')):>7}  "
            f"{s.get('peer') or '-'}"
        )
    if offline and not show_all:
        print(f"({len(offline)} further station(s) known but not connected — "
              f"use --list-all / '/stations all' to see them)")


def send_cmd(station: int, cmd: str, ctx, body_timeout: Optional[float]) -> Optional[str]:
    """POST one command; return the response text, or None on a handled error."""
    http_timeout = TRANSFER_HTTP_TIMEOUT if is_download_class(cmd) else NORMAL_HTTP_TIMEOUT
    body: dict = {"cmd": cmd}
    if body_timeout is not None:
        body["timeout"] = body_timeout
        http_timeout = body_timeout + 10.0
    if is_download_class(cmd):
        print("  (download-class command — device pauses, transfers, reconnects; this can take a while)")
    try:
        result = _post(f"/stream/{station}/cli", body, ctx, http_timeout)
        return result.get("response", "")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except (ValueError, AttributeError):
            pass
        print(f"  ! HTTP {e.code}: {detail}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"  ! could not reach {ADMIN_URL}: {e.reason}", file=sys.stderr)
    except TimeoutError:
        print(f"  ! client timed out after {http_timeout:.0f}s", file=sys.stderr)
    return None


def resolve_config(url: Optional[str] = None, api_key: Optional[str] = None) -> None:
    """Set the module-level ADMIN_URL / API_KEY from flags > env > .env > defaults.

    Split out of main() so a second front-end (stream_menu.py) resolves the admin
    endpoint the *same* way rather than growing a second copy that drifts."""
    global ADMIN_URL, API_KEY
    env_file = load_env_file()

    # API key: flag > $API_KEY > .env > CONFIG default.
    API_KEY = api_key or os.getenv("API_KEY") or env_file.get("API_KEY") or API_KEY

    # Admin URL: flag > $STREAM_ADMIN_URL > built from .env's port > CONFIG default.
    # STREAM_HOST in .env is the *bind* address (often 0.0.0.0) — never a connect
    # target — so we always dial 127.0.0.1 and only take the port from .env.
    if url:
        ADMIN_URL = url
    elif os.getenv("STREAM_ADMIN_URL"):
        ADMIN_URL = os.getenv("STREAM_ADMIN_URL")
    elif env_file.get("STREAM_ADMIN_PORT"):
        ADMIN_URL = f"http://127.0.0.1:{env_file['STREAM_ADMIN_PORT']}"

    if API_KEY == "changeme":
        print("warning: no API key found (.env, $API_KEY, --api-key all absent) - "
              "using the 'changeme' placeholder; expect HTTP 401.", file=sys.stderr)


def is_connected(stations: list[dict], station: int) -> bool:
    return any(s["station_id"] == station and s.get("connected") for s in stations)


def is_known(stations: list[dict], station: int) -> bool:
    return any(s["station_id"] == station for s in stations)


def pick_station(stations: list[dict], want: Optional[int]) -> Optional[int]:
    """Resolve the active station: explicit --station, else the sole connected one.

    An explicit --station is *verified*, not just trusted: the server keeps a session
    per station id long after the socket is gone, so `--station 1010` on a station
    that went away used to open a prompt that could never reach a device — every
    command would sit there until the CLI timeout. Refuse up front instead."""
    connected = [s["station_id"] for s in stations if s.get("connected")]
    if want is not None:
        if want in connected:
            return want
        if is_known(stations, want):
            print(f"Station {want} is known to the server but NOT connected — "
                  f"no command can reach it.", file=sys.stderr)
        else:
            print(f"Station {want} is unknown to this server "
                  f"(wrong instance, or it never connected).", file=sys.stderr)
        if connected:
            print(f"Connected right now: {connected}", file=sys.stderr)
        return None
    if len(connected) == 1:
        return connected[0]
    if not connected:
        print("No connected stations — start the device (or fake_device.py) first.", file=sys.stderr)
    else:
        print(f"Multiple stations connected {connected}; pick one with --station.", file=sys.stderr)
    return None


def repl(station: int, ctx, body_timeout: Optional[float]) -> int:
    try:
        import readline  # noqa: F401 - line editing + history if available
    except ImportError:
        pass
    print(f"Connected to {ADMIN_URL}, station {station}. Type /help, /quit to exit.")
    while True:
        try:
            line = input(f"{station}> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            continue
        if not line:
            continue

        if line.startswith("/"):
            parts = line[1:].split()
            meta = parts[0].lower() if parts else ""
            if meta in ("quit", "exit", "q"):
                return 0
            if meta == "help":
                print(f"  device allowlist: {', '.join(ALLOWLIST)}")
                print("  meta: /stations [all], /station <id>, /help, /quit")
                continue
            if meta == "stations":
                show_all = len(parts) > 1 and parts[1].lower() in ("all", "-a", "--all")
                try:
                    print_stations(fetch_stations(ctx), show_all=show_all)
                except (urllib.error.URLError, urllib.error.HTTPError) as e:
                    print(f"  ! {e}", file=sys.stderr)
                continue
            if meta == "station":
                if len(parts) == 2 and parts[1].isdigit():
                    target = int(parts[1])
                    # Re-fetch rather than trust a list from REPL start: a station may
                    # have dropped (or come back) while the prompt sat idle.
                    try:
                        current = fetch_stations(ctx)
                    except (urllib.error.URLError, urllib.error.HTTPError) as e:
                        print(f"  ! {e}", file=sys.stderr)
                        continue
                    if is_connected(current, target):
                        station = target
                        print(f"  active station -> {station}")
                    elif is_known(current, target):
                        print(f"  ! station {target} is known but not connected — "
                              f"staying on {station}", file=sys.stderr)
                    else:
                        print(f"  ! station {target} is unknown to this server — "
                              f"staying on {station}", file=sys.stderr)
                else:
                    print("  usage: /station <id>", file=sys.stderr)
                continue
            print(f"  ! unknown meta-command /{meta} (try /help)", file=sys.stderr)
            continue

        response = send_cmd(station, line, ctx, body_timeout)
        if response is not None:
            sys.stdout.write(response if response.endswith("\n") else response + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interactive terminal for the streaming server's remote CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Zero setup on the server host: API_KEY and the admin port are read "
               "from the repo .env. Override with --url/--api-key or the "
               "STREAM_ADMIN_URL/API_KEY env vars.",
    )
    parser.add_argument("cmd", nargs="*", help="One-shot command (omit for interactive REPL)")
    parser.add_argument("--station", type=int, default=None, help="Station id")
    parser.add_argument("--url", default=None, help="Admin base URL (default: from .env)")
    parser.add_argument("--api-key", default=None, help="X-API-Key (default: from .env)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="Server-side wait for the response, seconds (default: server's own)")
    parser.add_argument("--list", action="store_true",
                        help="List the connected stations and exit")
    parser.add_argument("--list-all", action="store_true",
                        help="Like --list, but also the stations the server only "
                             "remembers (connected=False)")
    args = parser.parse_args()

    resolve_config(args.url, args.api_key)

    ctx = ssl_context()

    try:
        stations = fetch_stations(ctx)
    except urllib.error.HTTPError as e:
        print(f"FAILED: HTTP {e.code} {e.reason} (check API key)", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"FAILED: could not reach {ADMIN_URL}: {e.reason}", file=sys.stderr)
        return 1

    if args.list or args.list_all:
        print_stations(stations, show_all=args.list_all)
        return 0

    station = pick_station(stations, args.station)
    if station is None:
        # Nothing to send to — show the full picture here, including the
        # remembered-but-offline stations: that is exactly the context needed
        # to tell "wrong server" from "device not connected right now".
        print_stations(stations, show_all=True)
        return 2

    # One-shot mode: everything after the flags is a single command.
    if args.cmd:
        response = send_cmd(station, " ".join(args.cmd), ctx, args.timeout)
        if response is None:
            return 1
        sys.stdout.write(response if response.endswith("\n") else response + "\n")
        return 0

    return repl(station, ctx, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
