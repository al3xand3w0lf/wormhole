#!/usr/bin/env python3
"""
test_download.py — simple download test client for the IoT File Server.

Lists the files the server offers for download, or fetches one of them via the
`GET /modem/download/{filename}` endpoint and saves it locally.

Examples:
    python test_download.py                          # list available downloads
    python test_download.py --filename config.bin    # download to ./config.bin
    python test_download.py --filename config.bin --out /tmp/cfg.bin
    python test_download.py --url https://host:8443 --insecure --list

The API key is resolved from (in order): --api-key, the API_KEY environment
variable, an API_KEY=... line in a local .env file, then the default "changeme".
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


def resolve_api_key(explicit: Optional[str]) -> str:
    """Find the API key from CLI arg, environment, or a local .env file."""
    if explicit:
        return explicit
    if os.getenv("API_KEY"):
        return os.environ["API_KEY"]
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if line.startswith("API_KEY=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "changeme"


def make_ssl_context(insecure: bool) -> Optional[ssl.SSLContext]:
    """Return an SSL context that skips verification (for self-signed certs)."""
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def list_downloads(url: str, api_key: str, context: Optional[ssl.SSLContext]) -> str:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/modem/download", headers={"X-API-Key": api_key}
    )
    with urllib.request.urlopen(req, context=context, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def download(url: str, api_key: str, filename: str,
             context: Optional[ssl.SSLContext]):
    endpoint = f"{url.rstrip('/')}/modem/download/{urllib.parse.quote(filename)}"
    req = urllib.request.Request(endpoint, headers={"X-API-Key": api_key})
    with urllib.request.urlopen(req, context=context, timeout=60) as resp:
        return resp.status, resp.headers, resp.read()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a file from the IoT File Server.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000",
                        help="Base server URL")
    parser.add_argument("--api-key", default=None,
                        help="API key (default: API_KEY env / .env / 'changeme')")
    parser.add_argument("--filename", default=None,
                        help="File to download (omit to just list available files)")
    parser.add_argument("--out", default=None,
                        help="Local path to save to (default: ./<filename>)")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS certificate verification (self-signed certs)")
    parser.add_argument("--list", action="store_true",
                        help="List available downloads (implied when no --filename)")
    args = parser.parse_args()

    api_key = resolve_api_key(args.api_key)
    context = make_ssl_context(args.insecure)

    # No filename -> just list what the server offers.
    if not args.filename or args.list:
        print("Available downloads:")
        try:
            print(list_downloads(args.url, api_key, context))
        except urllib.error.HTTPError as e:
            print(f"FAILED: HTTP {e.code} {e.reason}", file=sys.stderr)
            print(e.read().decode("utf-8", "replace"), file=sys.stderr)
            return 1
        except urllib.error.URLError as e:
            print(f"FAILED: could not reach server: {e.reason}", file=sys.stderr)
            return 1
        if not args.filename:
            return 0

    out_path = Path(args.out) if args.out else Path(args.filename)
    print(f"\nDownloading '{args.filename}' from {args.url}/modem/download/ -> {out_path}")

    try:
        status, headers, data = download(args.url, api_key, args.filename, context)
    except urllib.error.HTTPError as e:
        print(f"FAILED: HTTP {e.code} {e.reason}", file=sys.stderr)
        print(e.read().decode("utf-8", "replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"FAILED: could not reach server: {e.reason}", file=sys.stderr)
        return 1

    out_path.write_bytes(data)
    print(f"OK: HTTP {status} — saved {len(data):,} bytes to {out_path}")
    if headers.get("X-Request-ID"):
        print(f"X-Request-ID: {headers['X-Request-ID']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
