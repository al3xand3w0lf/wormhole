#!/usr/bin/env python3
"""
test_upload.py — simple upload test client for the IoT File Server.

Uploads a file to the server's `POST /modem/upload` endpoint (raw binary body)
and prints the result. If no file is given, a small test file is generated, so
you can verify a fresh deployment with a single command.

Examples:
    python test_upload.py                          # generate a test file, upload to localhost:8000
    python test_upload.py --file mydata.bin        # upload an existing file
    python test_upload.py --url https://host:8443 --insecure   # HTTPS with a self-signed cert
    python test_upload.py --list                   # also list the server's uploads afterwards

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
from datetime import datetime, timezone
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


def upload(url: str, api_key: str, device_id: str, filename: str,
           data: bytes, context: Optional[ssl.SSLContext]):
    query = urllib.parse.urlencode({"device_id": device_id, "filename": filename})
    endpoint = f"{url.rstrip('/')}/modem/upload?{query}"
    req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "X-API-Key": api_key,
            "Content-Type": "application/octet-stream",
        },
    )
    with urllib.request.urlopen(req, context=context, timeout=60) as resp:
        return resp.status, resp.headers, resp.read().decode("utf-8", "replace")


def list_uploads(url: str, api_key: str, context: Optional[ssl.SSLContext]) -> str:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/uploads", headers={"X-API-Key": api_key}
    )
    with urllib.request.urlopen(req, context=context, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload a test file to the IoT File Server.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000",
                        help="Base server URL")
    parser.add_argument("--api-key", default=None,
                        help="API key (default: API_KEY env / .env / 'changeme')")
    parser.add_argument("--device-id", default="testdevice",
                        help="device_id query parameter")
    parser.add_argument("--file", default=None,
                        help="Path to an existing file to upload (default: generate one)")
    parser.add_argument("--filename", default=None,
                        help="Target filename on the server (default: derived)")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS certificate verification (self-signed certs)")
    parser.add_argument("--list", action="store_true",
                        help="List the server's uploads after the upload")
    args = parser.parse_args()

    api_key = resolve_api_key(args.api_key)
    context = make_ssl_context(args.insecure)

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
          f"(device_id={args.device_id}) -> {args.url}/modem/upload")

    try:
        status, headers, body = upload(
            args.url, api_key, args.device_id, filename, data, context
        )
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        print(f"FAILED: HTTP {e.code} {e.reason}", file=sys.stderr)
        print(detail, file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"FAILED: could not reach server: {e.reason}", file=sys.stderr)
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
            print(list_uploads(args.url, api_key, context))
        except urllib.error.URLError as e:
            print(f"(could not list uploads: {e})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
