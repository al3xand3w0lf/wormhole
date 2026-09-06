#!/usr/bin/env python3
"""Fake device — drives the streaming server without hardware.

Emulates a device in streaming mode: connects, sends IDENT, then
streams UBX-RXM-RAWX + RTCM3 + sensor frames. It also answers CMD_REQUEST frames,
so the whole CLI control plane can be exercised end to end.

A download-class command is served the way a real device does it: FILE_REQUEST,
then FILE_BEGIN, then exactly `total` RAW bytes off the same socket, CRC32
checked. `--legacy-download` replays the old dance instead (ack -> disconnect
-> reconnect -> deferred answer), which is what a device with
`streaming_file_transfer = 0` still does.

Usage:
    python fake_device.py                       # localhost:9000, station 1001
    python fake_device.py --host 1.2.3.4 --station 1002 --secret s3cret
    python fake_device.py --legacy-download     # pre-B1 behaviour

    # Exercise rover auto-discovery against a real base without hardware -
    # run two instances with ports/station ids that don't collide.
    python fake_device.py --station 1001 --role base    # a base (its RTCM3 "1005"
                                                          # is synthetic/random, see
                                                          # rtcm3() - ARP decodes to
                                                          # noise, good enough to prove
                                                          # the wiring, not the geometry)
    python fake_device.py --station 1010 --role rover \\
        --rover-fix 47.400298,8.450366,459.4             # an arbitrary reference point
"""

import argparse
import random
import socket
import struct
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone

from pyrtcm import crc2bytes
from pyubx2 import isvalid_checksum

from pyubx2 import GET, UBXMessage

from streaming.frames import (
    PRIVATE_CLASS,
    ID_CLI_RESPONSE,
    ID_FILE_BEGIN,
    ID_FILE_REQUEST,
    ID_FILE_STATUS,
    ID_FILE_UP_BEGIN,
    ID_FILE_UP_DATA,
    ID_HEARTBEAT,
    ID_IDENT,
    ID_RTCM_DATA,
    ID_RTCM_INFO,
    ID_SENSOR_INA219,
    ID_SENSOR_SHT4X,
    FILE_PHASE_ABORTED,
    FILE_PHASE_ACCEPTED,
    FILE_PHASE_DONE,
    ROLE_BASE,
    ROLE_LOGGER,
    ROLE_ROVER,
    ROLE_STREAM,
    ROLE_UNSET,
    build_ubx,
    ubx_payload,
)
from streaming.framer import StreamFramer
from streaming.frames import ID_CMD_REQUEST

ROLE_NAMES = {"unset": ROLE_UNSET, "base": ROLE_BASE, "rover": ROLE_ROVER,
              "logger": ROLE_LOGGER, "stream": ROLE_STREAM}

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
# A real receiver reports GPS time, which currently runs 18 s ahead of UTC. The
# firmware never corrects for it, so its RTC (and hence rtc_unix) is GPS time too.
GPS_LEAP_S = 18
ALLOWLIST = ("whoami", "sysinfo", "listfiles", "download", "downloadcf", "downloadfw",
             "upload", "reboot", "fsdcard")

# Payload bytes per FILE_UP_DATA frame — mirrors STREAMING_FILE_UP_CHUNK.
FILE_UP_CHUNK = 1000


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


def rtcm3(msg_type: int = 1005, size: int = 20, ref_id: int = 0) -> bytes:
    """A frame with a real DF002/DF003 header and a random body.

    DF003 defaults to 0 - what a receiver on old u-blox firmware puts on the wire
    when it ignores the reference station id configured on it, and the case
    streaming/rtcm.py fills in. Pass a number to play a receiver that names
    itself. (Before this took an argument the id was part of the random tail,
    i.e. a different station on every frame - which no receiver ever sends.)
    """
    header = (msg_type << 12) | (ref_id & 0xFFF)
    payload = header.to_bytes(3, "big") + bytes(
        random.getrandbits(8) for _ in range(size)
    )
    body = bytes([0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF]) + payload
    return body + crc2bytes(body)


def ident(station_id: int, role: int = ROLE_UNSET) -> bytes:
    return build_ubx(PRIVATE_CLASS, ID_IDENT, struct.pack("<IB3x", station_id, role))


def navpvt(lat: float, lon: float, height_mm: int, fix_type: int = 3) -> bytes:
    """A synthetic UBX-NAV-PVT 3D fix - what --role rover sends periodically so
    a fake rover can exercise RoverAutoDiscovery end to end. height is raw mm,
    same as the real receiver (see streaming/geo.py's docstring on this trap)."""
    return UBXMessage("NAV", "NAV-PVT", GET, lat=lat, lon=lon, height=height_mm,
                      fixType=fix_type).serialize()


def cli_response(text: str, last: bool) -> bytes:
    return build_ubx(PRIVATE_CLASS, ID_CLI_RESPONSE, bytes([1 if last else 0]) + text.encode())


def file_request(name: str) -> bytes:
    nm = name.encode("ascii")
    return build_ubx(PRIVATE_CLASS, ID_FILE_REQUEST, bytes([0, len(nm)]) + nm)


def file_status(phase: int, code: int, nbytes: int) -> bytes:
    return build_ubx(PRIVATE_CLASS, ID_FILE_STATUS, struct.pack("<BbI", phase, code, nbytes))


def find_file_begin(buf: bytes):
    """Locate a complete FILE_BEGIN frame. Returns (total, crc32, name, end) or None.

    Mirrors stream_awaitFileBegin() in the firmware: FILE_BEGIN and the first
    payload bytes routinely share one read, so whatever sits behind `end` is
    already file content.
    """
    head = bytes([0xB5, 0x62, PRIVATE_CLASS, ID_FILE_BEGIN])
    i = buf.find(head)
    if i < 0 or len(buf) < i + 8:
        return None
    plen = buf[i + 4] | (buf[i + 5] << 8)
    end = i + 6 + plen + 2
    if len(buf) < end:
        return None                       # frame split across reads - wait for more
    raw = bytes(buf[i:end])
    if not isvalid_checksum(raw):
        return None
    payload = ubx_payload(raw)
    total, crc, name_len = struct.unpack_from("<IIB", payload, 0)
    name = payload[9 : 9 + name_len].decode("ascii", "replace")
    return total, crc, name, end


def target_files(cmd: str) -> list[str]:
    """Which server-side file(s) a download command asks for.

    Mirrors the firmware: downloadcf probes lowercase before uppercase,
    downloadfw asks for the encrypted image.
    """
    if cmd.startswith("downloadcf"):
        return ["config.txt", "CONFIG.TXT"]
    if cmd.startswith("downloadfw"):
        return ["device.bin"]
    parts = cmd.split(None, 1)
    return [parts[1].strip()] if len(parts) > 1 else ["CONFIG.TXT"]


def receive_file(sock: socket.socket, framer: StreamFramer, name: str) -> tuple[bool, str]:
    """Run one B1 transfer. Returns (ok, human-readable result)."""
    sock.sendall(file_request(name))
    print(f"-> FILE_REQUEST {name!r}")

    # Bytes already pulled off the socket but not yet consumed into a frame are
    # the first bytes of the answer — the firmware carries the same leftovers.
    buf = bytearray(framer.take_pending())

    old_timeout = sock.gettimeout()
    sock.settimeout(5.0)
    try:
        begin = None
        deadline = time.time() + 30
        while begin is None and time.time() < deadline:
            begin = find_file_begin(buf)
            if begin is not None:
                break
            chunk = sock.recv(65536)
            if not chunk:
                return False, "transfer failed: connection closed\r\n"
            buf.extend(chunk)
        if begin is None:
            return False, "transfer failed: no FILE_BEGIN\r\n"

        total, crc_expected, fname, end = begin
        del buf[:end]                       # everything behind the frame is payload

        if total == 0:
            print(f"<- FILE_BEGIN {fname!r}: not found on server")
            sock.sendall(file_status(FILE_PHASE_ABORTED, -9, 0))
            return False, f"{fname}: not found on server\r\n"

        print(f"<- FILE_BEGIN {fname!r} total={total} crc32={crc_expected:08X}")
        sock.sendall(file_status(FILE_PHASE_ACCEPTED, 1, total))

        # Consume EXACTLY total bytes, counted — no framing, like the firmware.
        payload = bytearray(buf[:total])
        del buf[: len(payload)]
        started = time.time()
        while len(payload) < total:
            chunk = sock.recv(min(65536, total - len(payload)))
            if not chunk:
                sock.sendall(file_status(FILE_PHASE_ABORTED, -4, len(payload)))
                return False, f"{fname}: truncated at {len(payload)}/{total}\r\n"
            payload.extend(chunk)
        elapsed = time.time() - started

        crc = zlib.crc32(bytes(payload)) & 0xFFFFFFFF
        if crc != crc_expected:
            print(f"!! CRC mismatch: got {crc:08X}, want {crc_expected:08X}")
            sock.sendall(file_status(FILE_PHASE_ABORTED, -6, len(payload)))
            return False, f"{fname}: CRC mismatch\r\n"

        # Any surplus would mean the server wrote past `total` — that is what a
        # spliced frame looks like from here, so say so instead of ignoring it.
        if buf:
            print(f"!! {len(buf)} surplus byte(s) after the payload")

        print(f"<- {fname}: {total} bytes OK in {elapsed:.1f}s (CRC {crc:08X})")
        sock.sendall(file_status(FILE_PHASE_DONE, 1, total))
        return True, f"{fname}: {total} bytes downloaded, CRC OK\r\n"
    finally:
        sock.settimeout(old_timeout)


def fake_sd_file(name: str) -> bytes:
    """Stand-in for a file on the SD card.

    Deterministic from the name, and full of "OK"/"CONNECT"/CRLF so the transfer
    is exercised against exactly the byte patterns that used to desync the old
    text-matching modem parser (BUG-029).
    """
    seed = f"--- {name} ---\r\n".encode()
    body = bytearray()
    n = 0
    while len(body) < 40_000:
        body += seed + f"line {n}: ".encode() + bytes(range(256)) + b"\r\nOK\r\nCONNECT 1024\r\n"
        n += 1
    return bytes(body)


def file_up_begin(name: str, total: int, crc: int) -> bytes:
    nm = name.encode("ascii")
    payload = struct.pack("<IIB", total, crc, len(nm)) + nm
    return build_ubx(PRIVATE_CLASS, ID_FILE_UP_BEGIN, payload)


def file_up_data(seq: int, chunk: bytes) -> bytes:
    return build_ubx(PRIVATE_CLASS, ID_FILE_UP_DATA, struct.pack("<H", seq) + chunk)


def send_file_up(sock: socket.socket, name: str) -> tuple[bool, str]:
    """Push one 'SD file' to the server, the way the firmware does (Stage 2)."""
    body = fake_sd_file(name)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    print(f"-> FILE_UP_BEGIN {name!r} total={len(body)} crc32={crc:08X}")
    sock.sendall(file_up_begin(name, len(body), crc))

    seq = 0
    for off in range(0, len(body), FILE_UP_CHUNK):
        sock.sendall(file_up_data(seq, body[off : off + FILE_UP_CHUNK]))
        seq += 1
    sock.sendall(file_status(FILE_PHASE_DONE, 1, len(body)))
    print(f"-> {name}: {len(body)} bytes in {seq} frames")
    return True, f"{name}: {len(body)} bytes uploaded\r\n"


def handle_command(payload: bytes, secret: str) -> tuple[str, str]:
    """Mirror the device: token check, then allowlist.

    Returns (response_or_command, kind) where kind is "" for a plain command,
    "download" or "upload" for the transfer classes.
    """
    tok_len = payload[0]
    token = payload[1 : 1 + tok_len].decode("ascii", "replace")
    cmd = payload[1 + tok_len :].decode("ascii", "replace").strip()

    if secret and token != secret:
        return "auth failed\r\n", ""
    if not cmd:
        return "empty command\r\n", ""
    if not any(cmd == c or cmd.startswith(c + " ") for c in ALLOWLIST):
        # Mirrors the firmware: echo the rejected verb so a typo is
        # distinguishable from a deliberately blocked command. Leading token
        # only, printable ASCII only, bounded.
        verb = "".join(c if " " <= c < "\x7f" else "?" for c in cmd.split(" ")[0])[:23]
        return f"command not permitted: {verb}\r\n", ""
    if cmd.startswith("download"):
        return cmd, "download"
    if cmd.startswith("upload"):
        return cmd, "upload"
    if cmd == "whoami":
        return "streaming station (fake device)\r\n", ""
    if cmd == "sysinfo":
        return "FW 1.51.2\r\nBL 1.8.0\r\nmode: streaming\r\n", ""
    return f"{cmd}: ok\r\n", ""


def run(host: str, port: int, station: int, secret: str, duration: float,
        legacy_download: bool = False, role: int = ROLE_UNSET,
        rover_fix: tuple[float, float, int] | None = None) -> int:
    framer = StreamFramer()
    deferred: str | None = None
    rtcm_in = 0        # RTCM_DATA envelopes received (B1)
    rtcm_bad = 0       # ... whose payload was not exactly one RTCM3 frame
    deadline = time.time() + duration
    last_navpvt = 0.0

    while time.time() < deadline:
        sock = socket.create_connection((host, port), timeout=5)
        sock.settimeout(0.5)
        print(f"connected to {host}:{port}")
        sock.sendall(ident(station, role))
        print(f"-> IDENT station {station} (role={role})")

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

            # --role rover: a periodic 3D fix, the one thing RoverAutoDiscovery
            # (streaming/rover_discovery.py) needs to auto-subscribe this
            # station to its nearest base. --rover-fix picks the position; the
            # RTCM3 sent above is otherwise ignored for a real rover, but sent
            # anyway so this stays one code path.
            if role == ROLE_ROVER and rover_fix is not None and time.time() - last_navpvt > 1.0:
                lat, lon, height_mm = rover_fix
                sock.sendall(navpvt(lat, lon, height_mm))
                last_navpvt = time.time()

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
                    # RTK corrections (B1). Mirrors what rtcm_rover.c does on the
                    # real device: strip the envelope, and the payload IS one
                    # whole RTCM3 frame. Checking that here is the point of the
                    # mirror — if the server ever packs two frames or half of one
                    # into an envelope, the firmware's only symptom is a rover
                    # that never fixes, which is unattributable from the outside.
                    if frame.cls_ == PRIVATE_CLASS and frame.id_ == ID_RTCM_DATA:
                        payload = ubx_payload(frame.raw)
                        rtcm_in += 1
                        ok = (len(payload) >= 6 and payload[0] == 0xD3
                              and 3 + (((payload[1] & 0x03) << 8) | payload[2]) + 3 == len(payload))
                        if not ok:
                            rtcm_bad += 1
                            print(f"<- RTCM_DATA MALFORMED: {len(payload)} B, "
                                  f"first={payload[:3].hex()}")
                        elif rtcm_in <= 3 or rtcm_in % 50 == 0:
                            mtype = (payload[3] << 4) | (payload[4] >> 4)
                            print(f"<- RTCM_DATA #{rtcm_in} type={mtype} "
                                  f"{len(payload)} B (bad so far: {rtcm_bad})")
                        continue

                    if frame.cls_ == PRIVATE_CLASS and frame.id_ == ID_RTCM_INFO:
                        payload = ubx_payload(frame.raw)
                        if len(payload) >= 2:
                            base_id = payload[0] | (payload[1] << 8)
                            print(f"<- RTCM_INFO base_id={base_id}")
                        continue

                    if frame.cls_ != PRIVATE_CLASS or frame.id_ != ID_CMD_REQUEST:
                        continue
                    payload = ubx_payload(frame.raw)
                    text, kind = handle_command(payload, secret)
                    if kind == "upload":
                        parts = text.split(None, 1)
                        target = parts[1].strip() if len(parts) > 1 else "CONFIG.TXT"
                        print(f"<- CMD {text!r} -> uploading over the stream")
                        _, result = send_file_up(sock, target)
                        sock.sendall(cli_response(result, True))
                        continue
                    is_download = kind == "download"
                    if is_download and legacy_download:
                        # Pre-B1 device (streaming_file_transfer = 0): the transfer
                        # runs over HTTP/FTP, so the socket has to go away first.
                        print(f"<- CMD '{text}' -> pausing stream for transfer")
                        sock.sendall(cli_response("stream paused for transfer\r\n", True))
                        time.sleep(0.2)
                        deferred = text
                        reconnect = True
                        break
                    if is_download:
                        # B1: the transfer rides this socket and answers live.
                        print(f"<- CMD {text!r} -> transferring over the stream")
                        result = ""
                        for candidate in target_files(text):
                            ok, result = receive_file(sock, framer, candidate)
                            if ok:
                                break        # downloadcf stops at the first hit
                        sock.sendall(cli_response(result, True))
                        continue
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
    p.add_argument("--legacy-download", action="store_true",
                   help="replay the pre-B1 download dance (disconnect + deferred answer)")
    p.add_argument("--role", choices=sorted(ROLE_NAMES), default="unset",
                   help="IDENT role byte, for exercising rover auto-discovery. "
                        "'rover' also needs --rover-fix to be useful.")
    p.add_argument("--rover-fix", metavar="LAT,LON,HEIGHT_M", default=None,
                   help="3D fix to report every second when --role rover, e.g. "
                        "47.400298,8.450366,459.4 (Schlieren ZH, the 2026-08-07 reference "
                        "point). Height in METRES here - converted to the raw mm NAV-PVT "
                        "sends on the wire.")
    args = p.parse_args()

    rover_fix = None
    if args.rover_fix:
        try:
            lat_s, lon_s, h_s = args.rover_fix.split(",")
            rover_fix = (float(lat_s), float(lon_s), round(float(h_s) * 1000))
        except ValueError:
            print(f"--rover-fix must be LAT,LON,HEIGHT_M, got {args.rover_fix!r}", file=sys.stderr)
            return 2
    elif args.role == "rover":
        print("--role rover with no --rover-fix: IDENT only, holds forever "
              "(RoverAutoDiscovery's hold-for-a-fix policy) unless it is the only known base.",
              file=sys.stderr)

    try:
        return run(args.host, args.port, args.station, args.secret, args.duration,
                   args.legacy_download, ROLE_NAMES[args.role], rover_fix)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
