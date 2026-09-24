#!/usr/bin/env python3
"""
gnss_tunnel.py — open, inspect and close a GNSS maintenance tunnel (K-01 Etappe 2).

Front-end over the admin API (:9001), exactly like stream_cli.py: it holds no
frame logic and never talks to a device. It asks the server to start a bridge
session on the station and to expose that session as a local TCP port, then tells
you what to point u-center at.

    python tools/gnss_tunnel.py open 1001              # normal: live link rate
    python tools/gnss_tunnel.py open 1001 --baud 921600
    python tools/gnss_tunnel.py open 1001 --rescue --baud 9600 --switch-baud 230400
    python tools/gnss_tunnel.py status
    python tools/gnss_tunnel.py close 1001

THERE IS NO GUI HERE, AND THERE WILL NOT BE ONE: u-center *is* the GUI. This
tool's entire job is to open the pipe and get out of the way.

─────────────────────────────────────────────────────────────────────────────
WHAT TO DO ON THE WINDOWS MACHINE, AND WHY
─────────────────────────────────────────────────────────────────────────────
1.  ssh -L <port>:127.0.0.1:<port> <server>
    The listener is bound to loopback on the server on purpose. A "temporarily
    open" port stays open; the SSH forward is how this server is administered
    anyway, so it costs nothing.

2.  Normal case, and the one proven over LTE (2026-09-17): legacy u-center
    DIRECTLY - Receiver -> Connection -> Network connection ->
    tcp://127.0.0.1:<port>. No virtual COM port needed.

    Only the ubxfwupdate command line (and therefore the rescue in step 4) needs
    a virtual COM port: HW VSP3, mode TCP Client, target localhost:<port>,
    NVT/RFC2217 OFF. The server recognises an RFC2217 peer by its opening
    negotiation and only then filters IAC sequences; a raw peer passes through
    verbatim, 0xFF included. (An unconditional filter ate 830 kB of a 1.4 MB
    test image - erased flash is nothing but 0xFF.)

3.  Normal update (healthy receiver), u-center Tools -> Firmware Update with
    "Use this baudrate for update" TICKED at the device's rate (921600),
    safeboot OFF, training sequence OFF - or the command line:
        ubxfwupdate.exe -p "\\\\.\\COM10" -b <rate>:<rate>:<rate> \\
                        -s 0 -t 0 -C 0 -v 1 <image.bin>

    ⚠ PIN THE RATE. All three numbers the same, and the same rate you passed to
    `open`. Unticking "use this baudrate for update" does NOT mean "do not
    switch" - it means "use my default, 460800". Over a cable u-center drags its
    own COM port along; over TCP THERE IS NO BAUD RATE TO DRAG, so only the
    receiver moves, the device's UART stays put, and the link dies at the moment
    the first byte is written.

    ⚠ -s 0 = no safeboot. SAFEBOOT_N is not wired out on this hardware, and the
    normal update does not need it.

4.  Rescue (receiver stuck in its boot ROM after a failed update):
        python tools/gnss_tunnel.py open <id> --rescue --baud 9600 --switch-baud 230400
        ubxfwupdate.exe -p "\\\\.\\COM10" -b 9600:9600:230400 \\
                        -t 1 -s 0 --no-fis 1 -C 0 -v 1 <image.bin>

    The device power-cycles the receiver and then stays SILENT: a fresh boot ROM
    binds its auto-baud to the first thing it hears, and that must be the tool's
    training sequence, not our baud scan. Six rescues failed on the bench before
    this was understood.

    ⚠ WHEN THE TOOL PRINTS "Setting baudrate to 230400", RUN THIS:
        python tools/gnss_tunnel.py switch <id>

    Do not wait for the device to notice by itself. It keys on the ~1.2 s pause
    the tool makes while reconfiguring its own port, and over LTE that pause does
    not survive: the modem delivers the downlink in bursts that swallow it, and
    the tool retries every 1.0 s against a 900 ms threshold, so each retry
    restarts the clock. Measured 2026-09-21 - tool switched at 4.7 s, gave up at
    7.9 s, device followed at ~8.8 s. Over a serial cable (UART3 transport) the
    automatic follow still works and no switch command is needed.

    If the run dies after the switch anyway, the flash loader is still alive:
    retry WITHOUT a new rescue (a power cycle would kill it), all three rates
    pinned to the download rate and no training sequence:
        ubxfwupdate.exe -p "\\.\COM10" -b 230400:230400:230400 -t 0 ...

    ⚠ 9600 CANNOT CARRY THE DOWNLOAD - it is only for the handshake. Measured:
    the tool pushes 1895 B/s into a 960 B/s line. That is what --switch-baud is
    for, and why leaving it out produces a session that dies after ~18 s.

5.  Ports at COM10 and above need the device path: -p "\\\\.\\COM10". Without it:
    "Could not open communications port".

⚠ WAIT ~40 s BETWEEN SESSIONS. Closing a session makes the device re-detect the
receiver (power cycle, baud scan, reconfiguration). Since FW 1.72.1 it refuses a
new session meanwhile and `open` reports that; older firmware accepted it and
lost the receiver (baud=0).

⚠ POWER. Start an update only on a device with secured VBAT/USB. A brown-out
mid-write is the one failure for which there is no documented way back.
"""

import argparse
import json
import sys
from pathlib import Path

# Reuse stream_cli's config resolution rather than growing a second copy that
# drifts out of step with it - the .env layout has already moved once.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stream_cli"))
import stream_cli  # noqa: E402


# Opening a tunnel starts a bridge session on the device, which quiesces its
# receiver tasks first; the reply comes back after that. Comfortably longer than
# the server's own CLI timeout so we are never the first to give up.
HTTP_TIMEOUT = 90.0


def _fmt(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True)


def cmd_open(args, ctx) -> int:
    body = {
        "baud": args.baud,
        "rescue": bool(args.rescue),
        "port": args.port,
    }
    if args.switch_baud:
        body["switch_baud"] = args.switch_baud
    if args.idle_s:
        body["idle_s"] = args.idle_s

    res = stream_cli._post(
        f"/stream/{args.station}/gnsstunnel/open", body, ctx, HTTP_TIMEOUT
    )
    tun = res.get("tunnel", {})
    port = tun.get("port")

    print(f"tunnel open for station {args.station}")
    print(f"  device said : {res.get('device_response', '').strip()}")
    print(f"  listening   : {tun.get('host')}:{port}")
    print()
    print("On the Windows machine:")
    print(f"  ssh -L {port}:127.0.0.1:{port} <server>")
    print(f"  virtual COM port -> TCP client -> localhost:{port}   (NVT/RFC2217 OFF)")
    if args.rescue:
        sw = args.switch_baud or 0
        print(f'  ubxfwupdate.exe -p "\\\\.\\COMxx" -b {args.baud}:{args.baud}:{sw} '
              f"-t 1 -s 0 --no-fis 1 -C 0 -v 1 <image.bin>")
        print()
        print("  The device is SILENT and waiting - your tool's training sequence")
        print("  must be the first thing the fresh boot ROM hears. Connect now.")
    else:
        b = args.baud if args.baud != "auto" else "<the rate shown by the device>"
        print(f'  ubxfwupdate.exe -p "\\\\.\\COMxx" -b {b}:{b}:{b} '
              f"-s 0 -t 0 -C 0 -v 1 <image.bin>")
        print()
        print("  Pin all three rates to the same value - over TCP nothing can")
        print("  follow a baud change on this side.")
    return 0


def cmd_close(args, ctx) -> int:
    res = stream_cli._post(
        f"/stream/{args.station}/gnsstunnel/close", {}, ctx, HTTP_TIMEOUT
    )
    stats = res.get("stats") or {}
    print(f"tunnel closed for station {args.station}")
    print(f"  device said : {str(res.get('device_response') or '').strip()}")
    if stats:
        print(f"  to_device   : {stats.get('to_device')} B")
        print(f"  to_operator : {stats.get('to_operator')} B")
        if stats.get("telnet_stripped"):
            print(f"  ⚠ telnet bytes stripped: {stats['telnet_stripped']} - "
                  "turn NVT/RFC2217 off in the virtual COM port")
    print()
    print("Cross-check the device's own counters - they must agree byte for byte:")
    print(f"  python stream_cli/stream_cli.py --station {args.station} 'gnssbridge status'")
    return 0


def cmd_switch(args, ctx) -> int:
    body = {}
    if args.baud:
        body["baud"] = args.baud
    res = stream_cli._post(
        f"/stream/{args.station}/gnsstunnel/switch", body, ctx, HTTP_TIMEOUT
    )
    print(f"switch requested for station {args.station}")
    print(f"  device said : {str(res.get('device_response') or '').strip()}")
    return 0


def cmd_status(args, ctx) -> int:
    res = stream_cli._get("/stream/gnsstunnel", ctx, HTTP_TIMEOUT)
    tunnels = res.get("tunnels", [])
    if not tunnels:
        print("no tunnel is open")
        return 0
    print(_fmt(tunnels))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Open/close a GNSS maintenance tunnel to a station's receiver.",
        epilog="Read the module docstring before the first update - the baud and "
               "RFC2217 traps in it each cost a bench day.",
    )
    ap.add_argument("--url", help="admin API base URL (default: from .env)")
    ap.add_argument("--api-key", help="API key (default: from .env)")

    sub = ap.add_subparsers(dest="action", required=True)

    p_open = sub.add_parser("open", help="start a session and bind a local port")
    p_open.add_argument("station", type=int)
    p_open.add_argument("--baud", default="auto",
                        help="receiver UART rate, or 'auto' for the live link rate "
                             "(default). NOT the boot baud-scan rate.")
    p_open.add_argument("--rescue", action="store_true",
                        help="receiver is in its boot ROM: power-cycle, then stay silent")
    p_open.add_argument("--switch-baud", type=int,
                        help="rescue only: rate the tool moves to for the download "
                             "(9600 cannot carry it)")
    p_open.add_argument("--idle-s", type=int,
                        help="end the device session after this long without operator "
                             "traffic (default: the device's own, 30 min for rescue)")
    p_open.add_argument("--port", type=int, default=0,
                        help="local port to bind (default: let the OS choose)")
    p_open.set_defaults(func=cmd_open)

    p_switch = sub.add_parser(
        "switch",
        help="move a running session to the download rate (do this when the "
             "update tool prints 'Setting baudrate to N')")
    p_switch.add_argument("station", type=int)
    p_switch.add_argument("--baud", type=int,
                          help="rate to move to; default: the --switch-baud the "
                               "tunnel was opened with")
    p_switch.set_defaults(func=cmd_switch)

    p_close = sub.add_parser("close", help="close the port and end the device session")
    p_close.add_argument("station", type=int)
    p_close.set_defaults(func=cmd_close)

    p_status = sub.add_parser("status", help="list open tunnels")
    p_status.set_defaults(func=cmd_status)

    args = ap.parse_args()
    stream_cli.resolve_config(args.url, args.api_key)
    ctx = stream_cli.ssl_context()

    try:
        return args.func(args, ctx)
    except Exception as exc:                      # noqa: BLE001 - operator tool
        # Show the server's reason, not just the status line: "409 Conflict"
        # alone hides whether the station is offline, a tunnel is already open,
        # or the device refused because its receiver is being recovered.
        detail = ""
        body = getattr(exc, "read", None)
        if callable(body):
            try:
                detail = " - " + json.loads(body().decode("utf-8", "replace")).get("detail", "")
            except Exception:                     # noqa: BLE001
                pass
        print(f"error: {exc}{detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
