#!/usr/bin/env python3
"""Fake device — drives the streaming server without hardware.

Emulates a device in streaming mode: connects, sends IDENT, then
streams UBX-RXM-RAWX + RTCM3 + sensor frames. It also answers CMD_REQUEST frames,
including the download dance (ack -> disconnect -> reconnect -> deferred answer),
so the whole CLI control plane can be exercised end to end.

Usage:
    python fake_device.py                       # localhost:9000, station 1001
    python fake_device.py --host 1.2.3.4 --station 1002 --secret s3cret
"""

import argparse
import random
import socket
import struct
import sys
import time
from datetime import datetime, timedelta, timezone

from pyrtcm import crc2bytes

from streaming.frames import (
    PRIVATE_CLASS,
    ID_CLI_RESPONSE,
    ID_HEARTBEAT,
    ID_IDENT,
    ID_SENSOR_INA219,
    ID_SENSOR_SHT4X,
    build_ubx,
    ubx_payload,
)
from streaming.framer import StreamFramer
from streaming.frames import ID_CMD_REQUEST

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
# A real receiver reports GPS time, which currently runs 18 s ahead of UTC. The
# firmware never corrects for it, so its RTC (and hence rtc_unix) is GPS time too.
GPS_LEAP_S = 18
ALLOWLIST = ("whoami", "sysinfo", "listfiles", "download", "downloadfw", "reboot", "fsdcard")


def now_week_tow() -> tuple[int, float]:
    """Current GPS time (UTC + leap seconds), as a real receiver would report it."""
    delta = datetime.now(timezone.utc) + timedelta(seconds=GPS_LEAP_S) - GPS_EPOCH
    total = delta.days * 86400 + delta.seconds + delta.microseconds / 1e6
    week = int(total // (7 * 86400))
    return week, total - week * 7 * 86400


def gps_rtc_unix() -> int:
    """What the device puts in its sensor frames: GPS time, encoded as unix seconds."""
    return int(time.time()) + GPS_LEAP_S


def rawx(week: int, tow: float, leap_s: int = GPS_LEAP_S) -> bytes:
    payload = struct.pack("<dHbBB3s", tow, week, leap_s, 0, 0, b"\x00\x00\x00")
    return build_ubx(0x02, 0x15, payload)


def rtcm3(msg_type: int = 1005, size: int = 20) -> bytes:
    payload = struct.pack(">H", (msg_type << 4) & 0xFFF0) + bytes(
        random.getrandbits(8) for _ in range(size)
    )
    body = bytes([0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF]) + payload
    return body + crc2bytes(body)


def ident(station_id: int) -> bytes:
    return build_ubx(PRIVATE_CLASS, ID_IDENT, struct.pack("<I", station_id) + b"\x00" * 4)


def cli_response(text: str, last: bool) -> bytes:
    return build_ubx(PRIVATE_CLASS, ID_CLI_RESPONSE, bytes([1 if last else 0]) + text.encode())


def handle_command(payload: bytes, secret: str) -> tuple[str, bool]:
    """Mirror the device: token check, then allowlist. Returns (response, is_download)."""
    tok_len = payload[0]
    token = payload[1 : 1 + tok_len].decode("ascii", "replace")
    cmd = payload[1 + tok_len :].decode("ascii", "replace").strip()

    if secret and token != secret:
        return "auth failed\r\n", False
    if not any(cmd == c or cmd.startswith(c + " ") for c in ALLOWLIST):
        return "command not permitted\r\n", False
    if cmd.startswith("download"):
        return cmd, True
    if cmd == "whoami":
        return "streaming station (fake device)\r\n", False
    if cmd == "sysinfo":
        return "FW 1.51.2\r\nBL 1.8.0\r\nmode: streaming\r\n", False
    return f"{cmd}: ok\r\n", False


def run(host: str, port: int, station: int, secret: str, duration: float) -> int:
    framer = StreamFramer()
    deferred: str | None = None
    deadline = time.time() + duration

    while time.time() < deadline:
        sock = socket.create_connection((host, port), timeout=5)
        sock.settimeout(0.5)
        print(f"connected to {host}:{port}")
        sock.sendall(ident(station))
        print(f"-> IDENT station {station}")

        if deferred is not None:
            time.sleep(0.3)
            sock.sendall(cli_response(f"{deferred}: transfer complete\r\n", True))
            print(f"-> deferred CLI answer for '{deferred}'")
            deferred = None

        last_sensor = 0.0
        reconnect = False

        while time.time() < deadline and not reconnect:
            week, tow = now_week_tow()
            sock.sendall(rawx(week, tow))
            sock.sendall(rtcm3(1005))
            sock.sendall(rtcm3(1077, 60))

            if time.time() - last_sensor > 3:
                sock.sendall(
                    build_ubx(
                        PRIVATE_CLASS,
                        ID_SENSOR_INA219,
                        struct.pack("<Iiii", gps_rtc_unix(), 12400, 85, 1054),
                    )
                )
                sock.sendall(
                    build_ubx(
                        PRIVATE_CLASS,
                        ID_SENSOR_SHT4X,
                        struct.pack("<Iii", gps_rtc_unix(), 21500, 45300),
                    )
                )
                sock.sendall(build_ubx(PRIVATE_CLASS, ID_HEARTBEAT, b""))
                last_sensor = time.time()

            # Poll for inbound CMD_REQUEST
            try:
                data = sock.recv(4096)
                if not data:
                    break
                for frame in framer.feed(data):
                    if frame.cls_ != PRIVATE_CLASS or frame.id_ != ID_CMD_REQUEST:
                        continue
                    payload = ubx_payload(frame.raw)
                    text, is_download = handle_command(payload, secret)
                    if is_download:
                        # Exactly what the reference firmware does.
                        print(f"<- CMD '{text}' -> pausing stream for transfer")
                        sock.sendall(cli_response("stream paused for transfer\r\n", True))
                        time.sleep(0.2)
                        deferred = text
                        reconnect = True
                        break
                    print(f"<- CMD -> {text.strip()!r}")
                    sock.sendall(cli_response(text, True))
            except socket.timeout:
                pass

            time.sleep(1.0)

        sock.close()
        if reconnect:
            print("disconnected for transfer, reconnecting in 2 s ...")
            time.sleep(2)
        else:
            break

    print("done")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Fake streaming device")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--station", type=int, default=1001)
    p.add_argument("--secret", default="")
    p.add_argument("--duration", type=float, default=60.0, help="seconds to run")
    args = p.parse_args()
    try:
        return run(args.host, args.port, args.station, args.secret, args.duration)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
