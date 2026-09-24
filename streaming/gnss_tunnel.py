"""GNSS maintenance tunnel: u-center on the operator's PC, a ZED in the field.

The device half is a serial bridge in the device firmware; this design rests on
bench measurements of that bridge.

WHAT THIS IS
------------
A local TCP listener that pipes raw bytes between one operator tool and one
station's GNSS receiver, wrapped in 0xF0 envelopes on the station leg:

    u-center / ubxfwupdate          (Windows, office)
        | virtual COM port -> TCP
    SSH port-forward                (the server is administered this way anyway)
        v
    127.0.0.1:<port>                THIS MODULE
        | ID_GNSS_TUNNEL_DOWN / _UP over the station's existing socket
        v
    device -> ublox_bridge.c -> UART1 -> ZED

Nothing here understands u-blox' flash protocol, and nothing needs to: it is not
public (released only under NDA) and nobody has reimplemented it. Every project
that can update a u-blox module - SparkFun RTK, ArduSimple, ArduPilot - runs
u-blox' own tool and tunnels the serial port to it. This is that tunnel.

TWO RULES THAT ARE NOT NEGOTIABLE
---------------------------------
1. NO BYTE MAY BE DROPPED. Every other downlink in this server is best-effort:
   rover.py evicts the oldest correction when a queue overflows, because a stale
   correction is worthless. Here a dropped byte is a bricked receiver. So this
   module never discards - it stops reading its own sockets and lets TCP
   backpressure travel to whichever end is too fast.

2. THE OPERATOR'S TOOL IS NOT A WELL-BEHAVED PEER. It was written for a direct
   serial cable: it retries three times at one-second intervals and gives up.
   Measured on the bench 2026-09-03: it pushed 1895 B/s into a 960 B/s line and
   17 kB of backlog killed the session. The pacing that matters is therefore the
   device's, and our job is only to not add buffering that hides it.

BOUND TO LOOPBACK, ALWAYS
-------------------------
A "temporarily open" port stays open. The operator reaches this through an SSH
forward - the same way the admin API on :9001 is reached - so the listener has
no business on a public interface.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .frames import TUNNEL_MAX_PAYLOAD, encode_gnss_tunnel

logger = logging.getLogger("streaming")

# How long a write into the station socket may block before we give up on it.
# Same value and same reasoning as rover.py: a device that cannot absorb a write
# in this long is gone, and hanging forever helps nobody.
WRITE_TIMEOUT_S = 5.0

# Read size from the operator socket. One envelope's worth, so a read never has
# to be split across two envelopes and the device never sees a partial one.
READ_CHUNK = TUNNEL_MAX_PAYLOAD


# ==========================================================================
# RFC 2217 / Telnet filtering
# ==========================================================================
# ⚠ THIS IS NOT OPTIONAL, and it was found the expensive way.
#
# Virtual COM port drivers (HW VSP3 and friends) negotiate port parameters
# IN BAND as Telnet IAC sequences. On the bench, 2026-09-04, the byte capture
# showed `FF FA 2C 01 00 00 25 80 FF F0` landing exactly between the boot ROM's
# CRC poll and its answer; the tool reported "Could not get ROM CRC" and the
# rescue failed. The bytes were not corruption - they were the driver talking to
# a server it assumed spoke RFC 2217.
#
# ⚠⚠ BUT THE FILTER MUST NOT RUN UNCONDITIONALLY, and that is the second half of
# the lesson - found by pushing a 1.4 MB image through this module on 2026-09-16.
#
# A client with RFC2217 turned OFF (which is what the operator is told to do)
# sends a literal 0xFF as ONE byte. The filter reads that as IAC and swallows it
# together with the byte after it: `41 FF 42 43` came out as `41 43`. A firmware
# image is full of 0xFF - erased flash is nothing else - so unconditional
# filtering corrupts exactly the regions that are supposed to be empty, and does
# it invisibly.
#
# So the filter is ARMED BY DETECTION, not by default: an RFC2217 driver always
# negotiates before it sends payload, so a session whose FIRST bytes are an IAC
# negotiation is a Telnet peer and every later `FF FF` in it is an escaped
# literal. A session that starts with anything else is raw, and stays raw for its
# whole life. Both modes are then internally consistent, which is the only
# property that makes either of them safe.
IAC = 0xFF
SB = 0xFA
SE = 0xF0

# IAC commands that open a negotiation: WILL, WONT, DO, DONT, SB.
_NEGOTIATION = (0xFB, 0xFC, 0xFD, 0xFE, SB)


def looks_like_telnet(first: bytes) -> bool:
    """Decide from a session's opening bytes whether the peer speaks RFC2217.

    Deliberately strict: only an IAC immediately followed by a negotiation verb
    counts. `FF FF` at the very start is NOT a negotiation - it is two erased
    bytes of a raw image, and treating it as Telnet would corrupt the file it
    came from.
    """
    return len(first) >= 2 and first[0] == IAC and first[1] in _NEGOTIATION


def strip_telnet(data: bytes, state: dict) -> bytes:
    """Remove Telnet/RFC2217 control sequences from an operator byte stream.

    Ported from the device firmware's hardware test tooling, where it was
    proven against the real driver. State is carried across calls in `state`
    because a sequence can straddle a read boundary.

    ⚠ The doubled-IAC rule is the subtle half: a real 0xFF byte is sent as
    `FF FF`, and a FIRMWARE IMAGE IS FULL OF 0xFF. Unescaping it wrongly (or not
    at all) corrupts erased-flash regions specifically - which would look like a
    device that flashes everything except the empty parts.
    """
    out = bytearray()
    for b in data:
        mode = state.get("mode", "data")

        if mode == "data":
            if b == IAC:
                state["mode"] = "iac"
            else:
                out.append(b)

        elif mode == "iac":
            if b == IAC:
                out.append(IAC)             # escaped literal 0xFF
                state["mode"] = "data"
            elif b == SB:
                state["mode"] = "sub"
            else:
                # Two-byte command (WILL/WONT/DO/DONT take one more byte).
                state["mode"] = "opt" if b in (0xFB, 0xFC, 0xFD, 0xFE) else "data"

        elif mode == "opt":
            state["mode"] = "data"          # swallow the option byte

        elif mode == "sub":
            if b == IAC:
                state["mode"] = "sub_iac"

        elif mode == "sub_iac":
            # IAC SE ends the sub-negotiation; IAC IAC is a literal inside it.
            state["mode"] = "data" if b == SE else "sub"

    return bytes(out)


def escape_telnet(data: bytes) -> bytes:
    """Double every 0xFF so a Telnet-speaking peer sees a literal, not an IAC.

    The exact inverse of the doubled-IAC rule in strip_telnet(), and it exists
    because that rule was implemented in ONE direction only until 2026-09-21.
    Operator -> device unescaped `FF FF` correctly; device -> operator wrote the
    receiver's bytes raw. A lone 0xFF from the receiver therefore reached an
    RFC2217 driver as the start of a command, and the driver swallowed the byte
    that followed it.

    ⚠ It held on the first LTE rescue by luck, not by design: 90 112 B came back
    from the receiver with NVT switched on at the operator's end and the image
    verified anyway. Nothing in that says the next session is safe - the bytes a
    boot ROM answers with are not ours to predict, and `ubxfwupdate` waits on
    exactly those acknowledgements.

    Applied ONLY to a peer that opened with a Telnet negotiation. For a raw peer
    this would be corruption of precisely the kind it prevents, which is why the
    caller passes the per-connection decision rather than a session-wide flag.
    """
    return data.replace(b"\xff", b"\xff\xff") if b"\xff" in data else data


def _dump_path(station_id: int, direction: str) -> str | None:
    """Where to append a raw copy of the tunnel traffic, or None when off.

    Diagnostic only, and OFF unless GNSS_TUNNEL_DUMP_DIR is set in the
    environment - this sits in the data path of a firmware transfer, and a
    feature that writes megabytes to disk by default is a feature that will one
    day be blamed for a failed flash.

    It exists because byte COUNTS cannot answer the question that matters. On
    2026-09-22 the device and the server agreed exactly (3736 = 3736, no drops)
    while `ubxfwupdate` reported CRC errors on the receiver's answers: the
    number of bytes was right and their content was not. A copy of the actual
    bytes splits that in half - if the UBX frames here already fail their
    checksum, the corruption happened at or before the device; if they are
    clean, it happened after the server.
    """
    # Switched on by the EXISTENCE OF THE DIRECTORY, deliberately not by an
    # environment variable: this service is started by systemd
    # (wormhole_9000-streaming.service), so an exported variable never reaches
    # it and a restart silently loses the setting. `mkdir data/tunnel_dumps`
    # turns it on, `rm -r` turns it off, and both survive a restart.
    base = os.environ.get("GNSS_TUNNEL_DUMP_DIR") or os.path.join("data", "tunnel_dumps")
    if not os.path.isdir(base):
        return None
    return os.path.join(base, f"tunnel_{station_id}_{direction}.bin")


def _dump(path: str | None, data: bytes) -> None:
    if not path:
        return
    try:
        with open(path, "ab") as fh:
            fh.write(data)
    except Exception:
        # Never let a diagnostic break the transfer it is observing.
        pass


@dataclass
class TunnelStats:
    """Byte counters, both directions, per session.

    These are the acceptance criterion, not decoration: what the operator sent
    must equal what the device received, and the device reports its own totals
    through `gnssbridge status`. Two independent counts that agree are the only
    proof this path kept its promise.
    """

    to_device: int = 0
    to_operator: int = 0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    telnet_stripped: int = 0
    # 0xFF bytes doubled on the way back to a Telnet-speaking operator. Counted
    # separately from to_operator on purpose: to_operator must stay comparable
    # with the DEVICE's own toPc, and that is the payload, not the wire.
    telnet_escaped: int = 0
    telnet_mode: bool | None = None
    operator_connects: int = 0


class GnssTunnel:
    """One tunnel session: one station, one local port, one operator at a time."""

    def __init__(self, session, host: str = "127.0.0.1", port: int = 0):
        self.session = session
        self.host = host
        self.port = port
        self.stats = TunnelStats()
        # Whether the CURRENTLY attached operator speaks Telnet; None until its
        # first bytes decide. Gates the outbound escaping in feed_from_device().
        self._operator_telnet: bool | None = None

        self._server: asyncio.AbstractServer | None = None
        self._operator_writer: asyncio.StreamWriter | None = None
        self._closing = False
        # Serialises "is there an operator" decisions against a connect racing a
        # close. Only one operator may ever be attached: two tools sharing one
        # receiver interleave their frames and neither can succeed.
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> int:
        """Bind the local listener. Returns the port actually bound."""
        self._server = await asyncio.start_server(
            self._handle_operator, self.host, self.port
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info(
            "station %s: GNSS tunnel listening on %s:%d",
            self.session.station_id, self.host, self.port,
        )
        return self.port

    async def stop(self) -> None:
        self._closing = True
        if self._operator_writer is not None:
            try:
                self._operator_writer.close()
            except Exception:
                pass
            self._operator_writer = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        logger.info(
            "station %s: GNSS tunnel closed (to_device=%d B, to_operator=%d B)",
            self.session.station_id, self.stats.to_device, self.stats.to_operator,
        )

    # ------------------------------------------------------- operator -> device

    async def _handle_operator(self, reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")

        async with self._lock:
            if self._operator_writer is not None:
                # Refuse rather than share. Two tools on one receiver is not a
                # degraded mode, it is a corrupted one.
                logger.warning(
                    "station %s: second operator %s refused - tunnel already in use",
                    self.session.station_id, peer,
                )
                writer.close()
                return
            self._operator_writer = writer
            self.stats.operator_connects += 1

        logger.info("station %s: operator %s attached to the GNSS tunnel",
                    self.session.station_id, peer)

        telnet_state: dict = {}
        telnet_mode: bool | None = None        # None = not decided yet
        # Per CONNECTION, not per session: the next operator may be a raw one,
        # and escaping its return stream would corrupt exactly what escaping is
        # meant to protect. self.stats.telnet_mode keeps the session's last
        # value for the status view; this one gates the wire.
        self._operator_telnet = None
        try:
            while not self._closing:
                data = await reader.read(READ_CHUNK)
                if not data:
                    break                      # operator closed the connection

                if telnet_mode is None:
                    telnet_mode = looks_like_telnet(data)
                    logger.info(
                        "station %s: tunnel peer speaks %s",
                        self.session.station_id,
                        "RFC2217/Telnet - control bytes will be filtered"
                        if telnet_mode else "raw bytes - passing through verbatim",
                    )
                    self.stats.telnet_mode = telnet_mode
                    self._operator_telnet = telnet_mode

                if telnet_mode:
                    clean = strip_telnet(data, telnet_state)
                    if len(clean) != len(data):
                        self.stats.telnet_stripped += len(data) - len(clean)
                else:
                    clean = data               # byte-transparent, 0xFF included
                if not clean:
                    continue

                # ⚠ AWAITED, not queued. This is where backpressure is created:
                # while the device is slow, this coroutine does not return to
                # read() and the operator's TCP window closes. A queue here - the
                # obvious "improvement" - would restore exactly the 17 kB backlog
                # that killed the bench run.
                await self._send_to_device(clean)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("station %s: GNSS tunnel operator loop failed",
                             self.session.station_id)
        finally:
            async with self._lock:
                if self._operator_writer is writer:
                    self._operator_writer = None
                    self._operator_telnet = None
            try:
                writer.close()
            except Exception:
                pass
            logger.info(
                "station %s: operator %s detached (to_device=%d B, to_operator=%d B)",
                self.session.station_id, peer,
                self.stats.to_device, self.stats.to_operator,
            )

    async def _send_to_device(self, data: bytes) -> None:
        """Envelope and write to the station, chunked, in order."""
        for off in range(0, len(data), TUNNEL_MAX_PAYLOAD):
            chunk = data[off:off + TUNNEL_MAX_PAYLOAD]
            frame = encode_gnss_tunnel(chunk)

            async with self.session.write_lock:
                # Re-read under the lock: waiting for it can take arbitrarily
                # long and unbind() clears the writer when the device drops.
                # Same hazard send_cli() and rover.py document.
                writer = self.session.writer
                if writer is None:
                    raise ConnectionError("station disconnected mid-tunnel")
                writer.write(frame)
                await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)

            self.stats.to_device += len(chunk)
            _dump(_dump_path(self.session.station_id, "down"), chunk)

    # ------------------------------------------------------- device -> operator

    def feed_from_device(self, data: bytes) -> None:
        """Called from the frame pipeline for every GNSS_TUNNEL_UP payload.

        Synchronous because route_frame() is - see pipeline.py. The write goes
        into the operator socket's own buffer; no drain is awaited here, and that
        asymmetry is deliberate: this direction carries the receiver's answers,
        which are small and which the tool is waiting for. Blocking the frame
        pipeline on a slow operator socket would stall the station's whole
        stream, including the CLI that ends the session.
        """
        if self._operator_writer is None:
            # No tool attached. The receiver chatters constantly, so this is the
            # normal state between connects and must not be logged per frame.
            return
        _dump(_dump_path(self.session.station_id, "up"), data)

        try:
            if self._operator_telnet:
                wire = escape_telnet(data)
                self.stats.telnet_escaped += len(wire) - len(data)
            else:
                wire = data
            self._operator_writer.write(wire)
            # The PAYLOAD length, never len(wire): this counter's whole job is to
            # be compared with the device's own toPc, and the device counts what
            # it sent, not what Telnet framing added on top.
            self.stats.to_operator += len(data)
        except Exception:
            logger.warning("station %s: GNSS tunnel write to operator failed",
                           self.session.station_id)

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        return {
            "station_id": self.session.station_id,
            "host": self.host,
            "port": self.port,
            "operator_attached": self._operator_writer is not None,
            "operator_connects": self.stats.operator_connects,
            "to_device": self.stats.to_device,
            "to_operator": self.stats.to_operator,
            "telnet_mode": self.stats.telnet_mode,
            "telnet_stripped": self.stats.telnet_stripped,
            "telnet_escaped": self.stats.telnet_escaped,
            "opened_at": self.stats.opened_at.isoformat(),
        }


class TunnelRegistry:
    """At most one tunnel per station, looked up by station id."""

    def __init__(self) -> None:
        self._tunnels: dict[int, GnssTunnel] = {}

    def get(self, station_id: int) -> GnssTunnel | None:
        return self._tunnels.get(station_id)

    async def open(self, session, host: str = "127.0.0.1", port: int = 0) -> GnssTunnel:
        existing = self._tunnels.get(session.station_id)
        if existing is not None:
            return existing
        tunnel = GnssTunnel(session, host, port)
        await tunnel.start()
        self._tunnels[session.station_id] = tunnel
        return tunnel

    async def close(self, station_id: int) -> bool:
        tunnel = self._tunnels.pop(station_id, None)
        if tunnel is None:
            return False
        await tunnel.stop()
        return True

    def all_status(self) -> list[dict]:
        return [t.status() for t in self._tunnels.values()]


# Module-level registry: the pipeline needs to reach a tunnel from a synchronous
# routing call, and threading it through every frame would touch every sink.
registry = TunnelRegistry()
