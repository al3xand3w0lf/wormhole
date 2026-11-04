#!/usr/bin/env python3
"""Minimal consumer for the raw stream out (streaming/rawout.py) - stdlib only.

Pulls one station's receiver output (UBX + RTCM3, unchanged) from a wormhole
server and writes it to stdout or a file, reconnecting on its own. Meant as a
working reference for whoever builds the processing side, and as a test tool:

    # NTRIP 2, to a file
    python3 tools/rawout_client.py wormhole.example.org:9003/1001 \
        --user processing --password @RAWOUT_PW -o 1001.ubx

    # a server with STREAM_RAWOUT_TLS=true and a self-signed certificate
    python3 tools/rawout_client.py host:9003/1001 --user u --password @PW --tls --cafile cert.pem

    # straight into a parser
    python3 tools/rawout_client.py host:9003/1001 --user u --password @PW | my_parser

    # list the stations this account may read
    python3 tools/rawout_client.py host:9003/ --user u --password @PW

Why not pygnssutils' GNSSNTRIPClient: it parses the stream with an RTCM3-only
(or SPARTN-only) filter, so every UBX frame is silently dropped. RTKLIB's
str2str passes everything through (but cannot do TLS, should a server use it).

A `@NAME` password is read from the environment variable NAME, so it does not
end up in the shell history or the process list.
"""

import argparse
import base64
import os
import socket
import ssl
import sys
import time


def _connect(host, port, use_tls, cafile, insecure, timeout):
    sock = socket.create_connection((host, port), timeout=timeout)
    if not use_tls:
        return sock
    ctx = ssl.create_default_context(cafile=cafile)
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(sock, server_hostname=host)


class _Reader:
    """Buffered reads on a socket, so the header and the data share one buffer."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("server closed the connection")
        self.buf += chunk

    def line(self):
        while b"\r\n" not in self.buf:
            self.fill()
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def exact(self, n):
        while len(self.buf) < n:
            self.fill()
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def some(self):
        if not self.buf:
            self.fill()
        data, self.buf = self.buf, b""
        return data


def session(args, out):
    sock = _connect(args.host, args.port, args.tls, args.cafile, args.insecure, args.timeout)
    rd = _Reader(sock)
    try:
        auth = base64.b64encode(f"{args.user}:{args.password}".encode()).decode()
        req = (f"GET /{args.mount} HTTP/1.1\r\nHost: {args.host}\r\n"
               f"Ntrip-Version: Ntrip/2.0\r\nUser-Agent: NTRIP wormhole-rawout-client/1.0\r\n"
               f"Authorization: Basic {auth}\r\nConnection: close\r\n\r\n")
        if args.ntrip1:
            req = (f"GET /{args.mount} HTTP/1.0\r\nUser-Agent: NTRIP wormhole-rawout-client/1.0\r\n"
                   f"Authorization: Basic {auth}\r\n\r\n")
        sock.sendall(req.encode())

        status = rd.line().decode("latin-1")
        headers = {}
        if not status.startswith("ICY"):
            while True:
                line = rd.line().decode("latin-1")
                if not line:
                    break
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()

        if status.startswith("SOURCETABLE") or "sourcetable" in headers.get("content-type", ""):
            try:
                while True:          # the server closes after the table
                    rd.fill()
            except ConnectionError:
                pass
            sys.stdout.write(rd.buf.decode("latin-1"))
            return "sourcetable"
        if not (status.startswith("ICY 200") or " 200 " in status):
            sys.stderr.write(f"refused: {status}\n")
            return "refused"

        sys.stderr.write(f"connected: {args.host}:{args.port}/{args.mount} ({status})\n")
        chunked = headers.get("transfer-encoding", "").lower() == "chunked"
        # No keepalive exists in a raw stream: silence means the station is
        # away, so the receive timeout is what decides a reconnect.
        sock.settimeout(args.timeout)
        while True:
            if chunked:
                size = int(rd.line().split(b";")[0], 16)
                if size == 0:
                    raise ConnectionError("server ended the stream")
                data = rd.exact(size)
                rd.exact(2)
            else:
                data = rd.some()
            out.write(data)
            out.flush()
    finally:
        sock.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", help="HOST:PORT/MOUNTPOINT (empty mountpoint = sourcetable)")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True, help="password, or @ENVVAR")
    ap.add_argument("-o", "--output", help="file to append to (default: stdout)")
    ap.add_argument("--tls", action="store_true", help="the server has STREAM_RAWOUT_TLS on")
    ap.add_argument("--cafile", help="with --tls: CA / self-signed certificate to trust")
    ap.add_argument("--insecure", action="store_true", help="do not verify the certificate")
    ap.add_argument("--ntrip1", action="store_true", help="speak NTRIP 1 instead of 2")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds without data before reconnecting")
    ap.add_argument("--retry", type=float, default=5.0, help="seconds between reconnects")
    args = ap.parse_args()

    hostport, _, args.mount = args.target.partition("/")
    args.host, _, port = hostport.rpartition(":")
    args.port = int(port)
    if args.password.startswith("@"):
        args.password = os.environ.get(args.password[1:], "")

    out = open(args.output, "ab") if args.output else sys.stdout.buffer
    while True:
        try:
            result = session(args, out)
            if result in ("sourcetable", "refused"):
                return 0 if result == "sourcetable" else 1
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001 - reconnect on anything
            sys.stderr.write(f"disconnected: {exc}\n")
        time.sleep(args.retry)


if __name__ == "__main__":
    sys.exit(main())
