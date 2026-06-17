#!/usr/bin/env python3
"""
test_upload.py — standalone upload tester for a wormhole / IoT File Server.

Self-contained: no .env and no third-party packages (Python standard library
only). Copy this file onto any machine that can reach the server, edit the three
CONFIG values below, then run it.

    python test_upload.py                  # upload a generated test file
    python test_upload.py --file data.bin  # upload an existing file
    python test_upload.py --list           # also list the server's uploads
"""

# ═══════════════════════════════════════════════════════════════════════
#  CONFIG — edit these to match your wormhole server
# ═══════════════════════════════════════════════════════════════════════
SERVER_URL = "http://127.0.0.1:8000"   # http(s)://<ip-or-host>:<port>
API_KEY    = "changeme"                 # API key configured on the server
VERIFY_TLS = False                      # ignored for http; set True to verify a real HTTPS cert
# ═══════════════════════════════════════════════════════════════════════

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def ssl_context() -> Optional[ssl.SSLContext]:
    """Skip certificate verification when VERIFY_TLS is False (self-signed certs)."""
    if VERIFY_TLS:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def generate_test_payload(device_id: str) -> bytes:
    now = datetime.now(timezone.utc).isoformat()
    nonce = os.urandom(8).hex()
    text = (
        "wormhole upload test\n"
        f"timestamp={now}\n"
        f"device_id={device_id}\n"
        f"nonce={nonce}\n"
    )
    return text.encode("utf-8")


def upload(device_id: str, filename: str, data: bytes,
           context: Optional[ssl.SSLContext]):
    query = urllib.parse.urlencode({"device_id": device_id, "filename": filename})
    endpoint = f"{SERVER_URL.rstrip('/')}/modem/upload?{query}"
    req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/octet-stream"},
    )
    with urllib.request.urlopen(req, context=context, timeout=60) as resp:
        return resp.status, resp.headers, resp.read().decode("utf-8", "replace")


def list_uploads(context: Optional[ssl.SSLContext]) -> str:
    req = urllib.request.Request(
        f"{SERVER_URL.rstrip('/')}/uploads", headers={"X-API-Key": API_KEY}
    )
    with urllib.request.urlopen(req, context=context, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload a test file to a wormhole / IoT File Server "
                    "(configure SERVER_URL/API_KEY at the top of this file).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--file", default=None,
                        help="Path to an existing file to upload (default: generate one)")
    parser.add_argument("--filename", default=None,
                        help="Target filename on the server (default: derived)")
    parser.add_argument("--device-id", default="testdevice",
                        help="device_id query parameter")
    parser.add_argument("--list", action="store_true",
                        help="List the server's uploads after the upload")
    args = parser.parse_args()

    context = ssl_context()

    if args.file:
        path = Path(args.file)
        if not path.is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2
        data = path.read_bytes()
        filename = args.filename or path.name
    else:
        data = generate_test_payload(args.device_id)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = args.filename or f"testfile_{ts}.txt"

    print(f"Uploading {len(data):,} bytes as '{filename}' "
          f"(device_id={args.device_id}) -> {SERVER_URL}/modem/upload")

    try:
        status, headers, body = upload(args.device_id, filename, data, context)
    except urllib.error.HTTPError as e:
        print(f"FAILED: HTTP {e.code} {e.reason}", file=sys.stderr)
        print(e.read().decode("utf-8", "replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"FAILED: could not reach {SERVER_URL}: {e.reason}", file=sys.stderr)
        return 1

    print(f"OK: HTTP {status}")
    if headers.get("X-Request-ID"):
        print(f"X-Request-ID: {headers['X-Request-ID']}")
    try:
        print(json.dumps(json.loads(body), indent=2))
    except json.JSONDecodeError:
        print(body)

    if args.list:
        print("\nServer uploads:")
        try:
            print(list_uploads(context))
        except urllib.error.URLError as e:
            print(f"(could not list uploads: {e})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
