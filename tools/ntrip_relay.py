#!/usr/bin/env python3
"""Relay one NTRIP mountpoint into one or more casters.

Pulls RTCM3 from a caster as an ordinary NTRIP client, then re-publishes the
same bytes as a source push (NTRIP 1.0 SOURCE) into every target given. Useful
to exercise caster infrastructure with a real, independent correction stream
before - or without - a station of your own pushing into it, and to re-publish
a mountpoint you may consume but not redistribute at its origin.

The push side reuses streaming.ntrip.NtripCasterSink, the same code path the
streaming server uses for its own stations, so the wire behaviour is identical
to production rather than a second implementation that might differ.

    # one source, two casters, mountpoint name kept
    tools/ntrip_relay.py --source ntrip.example.org:2101/BASE1 \
        --source-user you@example.org --source-pass none \
        --target 127.0.0.1:2101/BASE1:mountpoint-password \
        --target 127.0.0.1:2104/BASE1:@BKG_ENCODER_PASSWORD

A target password of the form @NAME is read from the environment variable NAME,
which keeps credentials out of the command line and the shell history.

The mountpoint must already exist on each target caster: provisioning is the
caster operator's business, and this tool never writes caster config. For the
bundled caster that means adding the station to STREAM_CASTER_STATIONS and
re-running caster/setup.sh.
"""

import argparse
import asyncio
import base64
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from streaming.ntrip import NtripCasterSink  # noqa: E402

_PULL_RECONNECT_DELAY = 5.0
_CONNECT_TIMEOUT = 10.0
_HANDSHAKE_TIMEOUT = 10.0
_IDLE_TIMEOUT = 60.0  # no bytes at all for this long -> assume the source died

logger = logging.getLogger("ntrip_relay")


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    mountpoint: str
    password: str


def parse_source(value: str) -> tuple[str, int, str]:
    """"host:port/mount" -> (host, port, mount)."""
    hostport, _, mount = value.partition("/")
    host, _, port = hostport.partition(":")
    if not (host and port.isdigit() and mount):
        raise argparse.ArgumentTypeError(
            f"expected host:port/mountpoint, got {value!r}")
    return host, int(port), mount


def parse_target(value: str) -> Target:
    """"host:port/mount:password" -> Target.

    Split left to right: a mountpoint cannot contain ":", so everything after
    the colon that follows it is the password - including any colons of its own.
    A password of "@NAME" is read from environment variable NAME.
    """
    hostport, _, rest = value.partition("/")
    host, _, port = hostport.partition(":")
    mount, _, password = rest.partition(":")
    if not (host and port.isdigit() and mount and password):
        raise argparse.ArgumentTypeError(
            f"expected host:port/mountpoint:password, got {value!r}")
    if password.startswith("@"):
        env_name = password[1:]
        resolved = os.getenv(env_name)
        if not resolved:
            raise argparse.ArgumentTypeError(
                f"target {mount!r}: environment variable {env_name} is unset or empty")
        password = resolved
    return Target(host, int(port), mount, password)


async def _pull_once(host: str, port: int, mount: str, user: str, password: str,
                     sinks: list[NtripCasterSink]) -> None:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=_CONNECT_TIMEOUT
    )
    try:
        auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        writer.write((
            f"GET /{mount} HTTP/1.0\r\n"
            f"User-Agent: NTRIP wormhole-relay/1.0\r\n"
            f"Authorization: Basic {auth}\r\n"
            f"\r\n"
        ).encode("ascii"))
        await writer.drain()

        status = (await asyncio.wait_for(reader.readline(), timeout=_HANDSHAKE_TIMEOUT)).strip()
        upper = status.upper()

        # A caster answers a pull it will not serve - unknown mountpoint, or one
        # with no source pushing right now - with its sourcetable, and that
        # status line says "200 OK" too. Checking for "200 OK" alone therefore
        # reports success and then relays "ENDSOURCETABLE" as if it were RTCM3.
        if upper.startswith(b"SOURCETABLE"):
            logger.warning(
                "pull: %s:%d has no live source on mountpoint %r right now "
                "(answered with its sourcetable, not the stream)", host, port, mount)
            return
        if not (upper.startswith(b"ICY 200") or b"200 OK" in upper):
            logger.warning("pull handshake rejected: %r", status)
            return

        # How the payload starts depends on which protocol version answered, and
        # getting this wrong loses the whole stream silently either way:
        #   ICY 200 OK  (NTRIP 1.0) - no headers at all, RTCM3 starts immediately.
        #     Waiting for a blank line here blocks forever, because a continuous
        #     binary stream essentially never contains a byte-exact empty line.
        #   HTTP/1.1 200 OK (NTRIP 2.0) - a normal header block follows and MUST
        #     be drained, or its text is relayed as if it were RTCM3.
        if not upper.startswith(b"ICY"):
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=_HANDSHAKE_TIMEOUT)
                if line in (b"\r\n", b"\n", b""):
                    break
        logger.info("pull: connected to %s:%d/%s", host, port, mount)

        total = 0
        while True:
            data = await asyncio.wait_for(reader.read(4096), timeout=_IDLE_TIMEOUT)
            if not data:
                logger.warning("pull: source closed the connection (%d bytes total)", total)
                return
            total += len(data)
            stamp = datetime.now(timezone.utc)
            for sink in sinks:
                sink.on_rtcm3(data, stamp, True)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - socket may already be dead
            pass


async def _pull_forever(host: str, port: int, mount: str, user: str, password: str,
                        sinks: list[NtripCasterSink]) -> None:
    while True:
        try:
            await _pull_once(host, port, mount, user, password, sinks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a source outage must never kill the relay
            logger.warning("pull from %s:%d/%s failed: %s", host, port, mount, exc)
        await asyncio.sleep(_PULL_RECONNECT_DELAY)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", required=True, type=parse_source,
                   metavar="HOST:PORT/MOUNT", help="caster and mountpoint to pull from")
    p.add_argument("--source-user", default="",
                   help="username for the pull (many casters ignore it, some reject an empty one)")
    p.add_argument("--source-pass", default="none", help="password for the pull")
    p.add_argument("--target", required=True, action="append", type=parse_target,
                   metavar="HOST:PORT/MOUNT:PASSWORD",
                   help="caster to push into; repeat for several. PASSWORD may be @ENVVAR")
    p.add_argument("--log", metavar="FILE", help="also write the log to this file")
    return p.parse_args(argv)


async def main_async(args: argparse.Namespace) -> None:
    host, port, mount = args.source
    sinks = [NtripCasterSink(t.host, t.port, t.mountpoint, t.password) for t in args.target]
    logger.info("relaying %s:%d/%s -> %s", host, port, mount,
                ", ".join(f"{t.host}:{t.port}/{t.mountpoint}" for t in args.target))
    try:
        await _pull_forever(host, port, mount, args.source_user, args.source_pass, sinks)
    finally:
        for sink in sinks:
            sink.close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log:
        handlers.append(logging.FileHandler(args.log))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
