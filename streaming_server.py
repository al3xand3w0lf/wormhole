#!/usr/bin/env python3
"""Streaming server.

Receives the live device stream (raw u-blox UBX + RTCM3, interleaved with the
device's private 0xF0 frames) over one persistent TCP socket per station, demuxes
it, and records it to files. Also exposes an admin/CLI HTTP API.

Runs alongside — and independently of — the batch HTTP file server (server.py).

Usage:
    python streaming_server.py                 # config from .env
    python streaming_server.py --port 9000
"""

import argparse
import asyncio
import sys

from streaming import config, server


def main() -> int:
    parser = argparse.ArgumentParser(description="Streaming server")
    parser.add_argument("--host", help="TCP bind host (default from .env)")
    parser.add_argument("--port", type=int, help="TCP data port (default from .env)")
    parser.add_argument("--admin-port", type=int, help="HTTP admin port (default from .env)")
    args = parser.parse_args()

    if args.host:
        config.STREAM_HOST = args.host
    if args.port:
        config.STREAM_PORT = args.port
    if args.admin_port:
        config.STREAM_ADMIN_PORT = args.admin_port

    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\nshutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
