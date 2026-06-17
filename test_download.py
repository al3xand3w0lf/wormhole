#!/usr/bin/env python3
"""
test_download.py — standalone download tester for a wormhole / IoT File Server.

Self-contained: no .env and no third-party packages (Python standard library
only). Copy this file onto any machine that can reach the server, edit the three
CONFIG values below, then run it.

    python test_download.py                          # list available downloads
    python test_download.py --filename config.bin    # download to ./config.bin
    python test_download.py --filename config.bin --out /tmp/cfg.bin
"""

# ═══════════════════════════════════════════════════════════════════════
#  CONFIG — edit these to match your wormhole server
# ═══════════════════════════════════════════════════════════════════════
SERVER_URL = "http://127.0.0.1:8000"   # http(s)://<ip-or-host>:<port>
API_KEY    = "changeme"                 # API key configured on the server
VERIFY_TLS = False                      # ignored for http; set True to verify a real HTTPS cert
# ═══════════════════════════════════════════════════════════════════════

import argparse
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
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


def list_downloads(context: Optional[ssl.SSLContext]) -> str:
    req = urllib.request.Request(
        f"{SERVER_URL.rstrip('/')}/modem/download", headers={"X-API-Key": API_KEY}
    )
    with urllib.request.urlopen(req, context=context, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def download(filename: str, context: Optional[ssl.SSLContext]):
    endpoint = f"{SERVER_URL.rstrip('/')}/modem/download/{urllib.parse.quote(filename)}"
    req = urllib.request.Request(endpoint, headers={"X-API-Key": API_KEY})
    with urllib.request.urlopen(req, context=context, timeout=60) as resp:
        return resp.status, resp.headers, resp.read()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a file from a wormhole / IoT File Server "
                    "(configure SERVER_URL/API_KEY at the top of this file).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--filename", default=None,
                        help="File to download (omit to just list available files)")
    parser.add_argument("--out", default=None,
                        help="Local path to save to (default: ./<filename>)")
    parser.add_argument("--list", action="store_true",
                        help="List available downloads (implied when no --filename)")
    args = parser.parse_args()

    context = ssl_context()

    # No filename -> just list what the server offers.
    if not args.filename or args.list:
        print("Available downloads:")
        try:
            print(list_downloads(context))
        except urllib.error.HTTPError as e:
            print(f"FAILED: HTTP {e.code} {e.reason}", file=sys.stderr)
            print(e.read().decode("utf-8", "replace"), file=sys.stderr)
            return 1
        except urllib.error.URLError as e:
            print(f"FAILED: could not reach {SERVER_URL}: {e.reason}", file=sys.stderr)
            return 1
        if not args.filename:
            return 0

    out_path = Path(args.out) if args.out else Path(args.filename)
    print(f"\nDownloading '{args.filename}' from {SERVER_URL}/modem/download/ -> {out_path}")

    try:
        status, headers, data = download(args.filename, context)
    except urllib.error.HTTPError as e:
        print(f"FAILED: HTTP {e.code} {e.reason}", file=sys.stderr)
        print(e.read().decode("utf-8", "replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"FAILED: could not reach {SERVER_URL}: {e.reason}", file=sys.stderr)
        return 1

    out_path.write_bytes(data)
    print(f"OK: HTTP {status} — saved {len(data):,} bytes to {out_path}")
    if headers.get("X-Request-ID"):
        print(f"X-Request-ID: {headers['X-Request-ID']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
