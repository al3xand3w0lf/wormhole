"""RTK rover correction downlink: whole RTCM3 frames out to rover stations.

This is the server-side counterpart to your device's RTCM rover-downlink
handler.

WHAT THIS IS AND IS NOT
-----------------------
It is a **router**, not a `Sink`. A sink receives from a station; corrections
have to go the other way and, for now, come from outside the fleet entirely:
until a base station exists in your own fleet, the source is a public NTRIP
caster (RTK2GO is a good free one to test against). Feeding rovers from
a base's `Sink.on_rtcm3()` is a five-line second source once a base exists —
`RoverRouter.publish()` is deliberately source-agnostic.

THE CONTRACT WITH THE FIRMWARE
------------------------------
**Exactly one RTCM3 frame per 0xF0 envelope.** Never two, never half of one.
That is what lets the STM32 stay free of any RTCM3 understanding: it strips the
envelope and hands the payload to the receiver unchanged. Breaking this here
does not produce a server error — it produces a rover that silently never fixes.

RTCM_INFO IS REPEATED, NOT ANNOUNCED ONCE
-----------------------------------------
The device learns which base it is working against only from `0x11 RTCM_INFO`,
and its copy can disappear without the server ever finding out — a reboot, a
`rover zero`, or simply that one envelope being discarded under the device's own
backlog limit. Every one of those leaves the rover reporting base `?0` while
receiving a perfectly good correction stream, and it looks from the outside like
the server never sent anything.

So the announcement goes out on every new connection, on every base change, and
on an interval on top of that. Three payload bytes twice a minute is not worth
being clever about.

FRESHNESS BEATS COMPLETENESS
----------------------------
Corrections are epoch-bound: 1077 and friends describe observations at a
specific GPS TOW, and the rover pairs them with its *own* observations of the
same epoch. A correction we are ten seconds late with is not "better than
nothing" — the receiver takes it without complaint and produces a degraded
solution that looks exactly like a good one from the outside.

So every per-station queue is bounded and drops the **oldest** on overflow.
Unlike the firmware (where AT+QIRD hands the oldest bytes back first and the
modem cannot be asked how much is queued), the right thing is implementable
here, so it is done.

THE TYPE FILTER BELONGS IN publish(), NOT IN THE SENDER
-------------------------------------------------------
The downlink is bandwidth-bound at the *device*, not here: the modem drains its
incoming buffer at a fixed rate, and everything offered above that rate turns
into backlog and discards. So a correction type the receiver cannot use is not
free — it costs a correction the receiver could have used.

Filtering therefore happens before the frame is queued. Doing it in
`_station_sender()` would be too late: `_queues` is bounded and drops the oldest
on overflow, so ballast that is thrown away at the far end has already evicted a
real correction on its way through.

Measured on a real base/rover pair: the
caster offers ~1730 B/s, the device drains ~1416 B/s, and the receiver reports
`RXM-RTCM msgUsed=1` — "not used" — for every 1137 (NavIC) it is sent. The result
was a rover that received 23 % of its epochs with no correction at all, one gap
of 29 s, and bursts of up to 32 frames when the backlog caught up. Whole frames,
correct CRC, attributed to the right base, and useless: RTK needs a stream that
tracks the rover's own epochs, not a complete one that arrives late.

Empty = pass everything, which is what a link with headroom wants. This is a
lever for a constrained link, not a default.

THE WRITE LOCK IS NOT OPTIONAL
------------------------------
`StationSession.write_lock` serialises everything written into the socket. A
file transfer writes raw, unframed bytes that the device consumes by byte count;
an RTCM envelope slipped between two payload writes would land *inside the file*.
That is the same splice hazard the download direction has, just reversed, and
`send_cli()` already takes the lock for the same reason. A correction stream at
1 Hz makes it far likelier than a CLI command ever did.

Taking the lock also means the correction stream pauses by itself for the
duration of a download, which is the behaviour we want anyway.

A WRITE THAT NEVER RETURNS IS THE FAILURE MODE THAT COSTS EVERYTHING
-------------------------------------------------------------------
The sender is one loop: take the next correction, write it, take the next. If
the write never completes, the loop stops there - and it stops *silently*. It
does not notice the disconnect, it does not notice the reconnect, and no counter
moves except the queue filling up. The downlink is then dead until the process
restarts, and nothing says so.

That is not hypothetical: on 2026-08-07 station 1001 froze at 10:12 mid-write,
survived a disconnect at 10:13 and a reconnect at 10:18, and was still parked on
the same write half an hour later, having sent nothing on the new socket.

So every write is bounded, and a write that times out costs the connection. The
device reconnects within seconds; a sender parked forever does not recover at
all. Dropping the socket is also what makes the timeout safe: the cancellation
can leave a partial envelope on the wire, and half an envelope would break the
one-frame-per-envelope contract above. A dead socket makes the torn frame moot.
"""

import asyncio
import base64
import logging
import time
from collections import Counter, deque

from .framer import KIND_RTCM3, StreamFramer
from .frames import encode_rtcm_data, encode_rtcm_info
from .sinks import Sink

logger = logging.getLogger("streaming")

# Corrections arrive at ~6/s and are epoch-bound: a write still unfinished after
# this long is not slow, it is stuck. Generous enough that a brief stall on a
# healthy link rides through, short enough that a dead one is noticed while the
# device is still trying to reconnect.
WRITE_TIMEOUT_S = 5.0

# How often the base announcement (RTCM_INFO) is repeated to a connected rover.
# Not a keepalive - see RoverRouter._due_announcement() for why once per
# connection is not enough.
RTCM_INFO_INTERVAL_S = 30.0


def _reference_station_id(raw: bytes) -> int | None:
    """Reference station id (DF003) of an RTCM3 frame, or None.

    Parsed with pyrtcm rather than by hand: the project rule is to use the
    library for protocol work, and a bit-offset written from memory is exactly
    the kind of thing that is wrong in a way nothing reports.
    """
    try:
        from pyrtcm import RTCMReader

        parsed = RTCMReader.parse(raw)
        value = getattr(parsed, "DF003", None)
        return int(value) if value is not None else None
    except Exception:  # noqa: BLE001 - not every message type carries DF003
        return None


def _message_type(raw: bytes) -> int | None:
    """RTCM3 message number (DF002) - the first 12 bits of the payload.

    Read straight from the header rather than through pyrtcm: this runs on every
    frame at both counting points, and DF002's position is fixed for every RTCM3
    message that exists. The library still does all real parsing.
    """
    if len(raw) < 6:
        return None
    return (raw[3] << 4) | (raw[4] >> 4)


class _TypeStats:
    """Per-message-type frame and byte counts, for one counting point."""

    def __init__(self):
        self.frames: Counter = Counter()
        self.bytes: Counter = Counter()

    def add(self, raw: bytes) -> None:
        t = _message_type(raw)
        if t is None:
            return
        self.frames[t] += 1
        self.bytes[t] += len(raw)

    def as_dict(self) -> dict:
        return {str(t): {"frames": n, "bytes": self.bytes[t],
                         "avg_bytes": round(self.bytes[t] / n, 1)}
                for t, n in sorted(self.frames.items())}


def _drop_connection(session) -> None:
    """Force a stuck session's socket shut so the device redials.

    Both steps matter: close() ends the connection, unbind() makes the station
    count as offline *now* rather than whenever the connection handler gets round
    to noticing. unbind() is writer-scoped, so if a newer connection has already
    taken over, this leaves it alone.
    """
    writer = session.writer
    if writer is None:
        return
    try:
        writer.close()
    except Exception:  # noqa: BLE001 - the socket may already be dead
        pass
    try:
        session.unbind(writer)
    except Exception:  # noqa: BLE001 - cleanup must not raise into the sender
        logger.debug("station: unbind after a stuck write failed", exc_info=True)


class RoverRouter:
    """Fans whole RTCM3 frames out to the stations configured as rovers.

    The rover set is mutable at runtime (`add_rover()` / `remove_rover()`) so a
    station can be subscribed or dropped without a process restart — either by
    an operator (`POST/DELETE /stream/rover/subscribe`) or by
    `rover_discovery.RoverAutoDiscovery` picking a nearest base. All mutation happens on
    the event loop that also runs `publish()` and the senders, and none of it
    crosses an `await`, so there is no lock: a running `for sid in
    self.station_ids` loop in `publish()` cannot be interleaved with a set
    mutation from a different coroutine.
    """

    def __init__(self, registry, station_ids: set[int], queue_len: int = 24,
                 rtcm_types: set[int] | None = None, base_station_id: int | None = None):
        self.registry = registry
        self.station_ids = set(station_ids)
        self.queue_len = queue_len
        # None or empty: forward every type. See the module docstring for why an
        # allowlist is the right shape and why it is applied before the queue.
        self.rtcm_types = set(rtcm_types) if rtcm_types else None
        # Which station this router's corrections come from, for logging/status
        # only — publish() itself is source-agnostic and never reads this.
        self.base_station_id = base_station_id

        # station id -> deque of raw RTCM3 frames, oldest dropped on overflow
        self._queues: dict[int, deque] = {sid: deque(maxlen=queue_len) for sid in self.station_ids}
        self._wake: dict[int, asyncio.Event] = {sid: asyncio.Event() for sid in self.station_ids}
        # station id -> (connected_since, base_id, monotonic time) of the last
        # RTCM_INFO actually written. connected_since is a fresh datetime on every
        # StationSession.bind(), so it identifies the connection - and unlike the
        # writer it is not a reference that would keep a dead socket alive.
        # See _due_announcement() for when this is considered stale.
        self._announced: dict[int, tuple] = {}
        # station id -> its running _station_sender() task. Populated for the
        # initial set by run(), and by add_rover() for anything added later.
        self._tasks: dict[int, asyncio.Task] = {}

        self.frames_in = 0
        self.frames_out = 0
        self.dropped_full = 0
        self.dropped_offline = 0
        self.dropped_filtered = 0

        # Message types counted at both ends of the router, because the pair is
        # what carries information: what the caster delivers, versus what a given
        # station actually gets written to it. A rover that never receives 1005
        # cannot produce a differential solution no matter how clean the
        # observation stream is, and the aggregate counters above cannot show
        # that - they count frames, not what is in them.
        self.types_in = _TypeStats()
        self.types_out: dict[int, _TypeStats] = {sid: _TypeStats() for sid in self.station_ids}

    # -- dynamic (un)subscription --------------------------------------------

    def add_rover(self, station_id: int) -> bool:
        """Start delivering to `station_id`. Idempotent; returns whether it changed anything.

        Safe to call before `run()` (queues up for the first `run()` to start)
        or after (starts the sender immediately) - `run()` only ever *adds*
        tasks for ids it has not already started, so the two paths converge.
        """
        if station_id in self.station_ids:
            return False
        self.station_ids.add(station_id)
        self._queues[station_id] = deque(maxlen=self.queue_len)
        self._wake[station_id] = asyncio.Event()
        self.types_out[station_id] = _TypeStats()
        self._spawn_sender(station_id)
        return True

    def remove_rover(self, station_id: int) -> bool:
        """Stop delivering to `station_id` and forget its state. Idempotent."""
        if station_id not in self.station_ids:
            return False
        self.station_ids.discard(station_id)
        task = self._tasks.pop(station_id, None)
        if task is not None:
            task.cancel()
        self._queues.pop(station_id, None)
        self._wake.pop(station_id, None)
        self.types_out.pop(station_id, None)
        self._announced.pop(station_id, None)
        return True

    def _spawn_sender(self, station_id: int) -> None:
        """Start `_station_sender(station_id)` if it is not already running.

        Requires a running event loop - true for every caller (run() itself,
        the admin endpoints, RoverAutoDiscovery), all of which execute on the
        server's loop.
        """
        if station_id in self._tasks:
            return
        self._tasks[station_id] = asyncio.ensure_future(self._station_sender(station_id))

    # -- source side --------------------------------------------------------

    def publish(self, raw: bytes) -> None:
        """Hand one whole RTCM3 frame to every rover. Source-agnostic."""
        self.frames_in += 1
        self.types_in.add(raw)

        # Counted above first: types_in stays "what the caster delivers", which
        # is what the pair with types_out is for. A frame whose type cannot be
        # read is not on any allowlist either - that is the right answer, not an
        # oversight.
        if self.rtcm_types is not None and _message_type(raw) not in self.rtcm_types:
            self.dropped_filtered += 1
            return

        for sid in self.station_ids:
            q = self._queues[sid]
            if len(q) == q.maxlen:
                self.dropped_full += 1      # deque evicts the oldest, which is right
            q.append(raw)
            self._wake[sid].set()

    # -- sink side ----------------------------------------------------------

    async def _station_sender(self, station_id: int) -> None:
        q = self._queues[station_id]
        wake = self._wake[station_id]

        while True:
            if not q:
                wake.clear()
                await wake.wait()
                continue

            raw = q.popleft()
            session = self.registry.get(station_id)

            if session is None or not session.connected or session.writer is None:
                self.dropped_offline += 1
                continue

            # A file transfer owns the socket for its whole duration. Queueing
            # behind it would deliver a burst of stale corrections the moment it
            # finished, which is worse than the gap.
            if session.file_transfer_active or session.transfer_in_progress:
                self.dropped_offline += 1
                continue

            base_id = _reference_station_id(raw)
            frames = []
            # Recorded only once the write succeeds - an announcement that never
            # reached the device must not be remembered as delivered, or the
            # device runs the whole session without knowing its base.
            announce = reason = None
            if base_id is not None:
                announce, reason = self._due_announcement(station_id, session, base_id)
                if announce is not None:
                    frames.append(encode_rtcm_info(base_id))

            try:
                frames.append(encode_rtcm_data(raw))
            except ValueError as exc:
                # Nothing legitimate exceeds the RTCM3 ceiling, so this is a
                # defect upstream - but one frame must not take the downlink
                # down with it. Before this was caught, the exception escaped
                # the task and every later correction was silently discarded.
                self.dropped_offline += 1
                logger.warning("station %s: refusing a malformed correction: %s",
                               station_id, exc)
                continue

            try:
                await asyncio.wait_for(self._write(session, frames), WRITE_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.dropped_offline += 1
                logger.warning(
                    "station %s: correction write stuck for %.0fs - dropping the "
                    "connection so the device can reconnect", station_id, WRITE_TIMEOUT_S)
                _drop_connection(session)
                continue
            except Exception as exc:  # noqa: BLE001 - a dead socket is normal here
                self.dropped_offline += 1
                logger.debug("station %s: correction write failed: %s", station_id, exc)
                continue

            self.frames_out += 1
            self.types_out[station_id].add(raw)
            if announce is not None:
                self._announced[station_id] = announce
                # A refresh every RTCM_INFO_INTERVAL_S would drown the log, so
                # only a real change is worth a line.
                log = logger.debug if reason == "refresh" else logger.info
                log("station %s: corrections from base %d (%s, to %s)",
                    station_id, announce[1], reason, session.peer)

    def _due_announcement(self, station_id: int, session, base_id: int):
        """(state to record, why) if RTCM_INFO is due for this station, else (None, None).

        Repeated on an interval, not just on a change. The device's copy can go
        away without the server ever learning of it: the single envelope can be
        discarded under the device's own backlog limit, `rover zero` clears the
        counters including the base, and a device that reboots without dropping
        the socket keeps the connection we key on. Each of those leaves the
        device reporting base `?0` while receiving a perfectly good stream, and
        a once-per-connection announcement has no way back from any of them.

        Cheap enough to be unconditional: 3 payload bytes twice a minute against
        a ~7/s correction stream.
        """
        prev = self._announced.get(station_id)
        now = time.monotonic()

        if prev is None or prev[0] != session.connected_since:
            reason = "new connection"
        elif prev[1] != base_id:
            reason = "base changed"
        elif now - prev[2] >= RTCM_INFO_INTERVAL_S:
            reason = "refresh"
        else:
            return None, None

        return (session.connected_since, base_id, now), reason

    @staticmethod
    async def _write(session, frames) -> None:
        async with session.write_lock:
            # Re-read the writer under the lock: bind() may have swapped or
            # cleared it while we waited. Same reason send_cli() does.
            writer = session.writer
            if writer is None:
                raise ConnectionError("writer gone while waiting for the write lock")
            for f in frames:
                writer.write(f)
            await writer.drain()

    async def run(self) -> None:
        """Run every currently-subscribed rover's sender, forever.

        Unlike the pre-2026-08-14 version, this does **not** return when
        `station_ids` is empty: a base with no rovers yet (dynamically
        registered, or waiting for RoverAutoDiscovery to place its first one)
        must still be a live task under server.py's `asyncio.gather()`, ready
        for `add_rover()` to hand it a sender. Senders started after this
        point (via add_rover()) are supervised the same way: they land in
        `self._tasks` and are cancelled here on shutdown.
        """
        for sid in list(self.station_ids):
            self._spawn_sender(sid)
        try:
            await asyncio.Event().wait()  # blocks until this task is cancelled
        finally:
            for task in list(self._tasks.values()):
                task.cancel()

    def status(self) -> dict:
        return {
            "rovers": sorted(self.station_ids),
            "rtcm_types": sorted(self.rtcm_types) if self.rtcm_types else None,
            "frames_in": self.frames_in,
            "frames_out": self.frames_out,
            "dropped_full": self.dropped_full,
            "dropped_offline": self.dropped_offline,
            "dropped_filtered": self.dropped_filtered,
            "queued": {sid: len(q) for sid, q in self._queues.items()},
            "types_in": self.types_in.as_dict(),
            "types_out": {str(sid): ts.as_dict() for sid, ts in self.types_out.items()},
        }


class RoverSourceSink(Sink):
    """Feeds a base station's RTCM3 straight into a RoverRouter (in-process).

    Attached to the base station's sink list, it turns every whole RTCM3 frame the
    base sends into a ``router.publish()`` - the in-fleet counterpart to
    ``ntrip_source()``, with no caster in the path. ``publish()`` is source-
    agnostic by design, so this is all the wiring a base needs; the one-frame-per-
    envelope contract, the type filter and the drop policy all live in the router.

    The base keeps its other sinks (archive, NTRIP push): this is purely additive.
    """

    def __init__(self, router: "RoverRouter"):
        self.router = router

    def on_rtcm3(self, raw: bytes, stamp, sysclk: bool) -> None:
        self.router.publish(raw)


async def ntrip_source(
    router: RoverRouter,
    host: str,
    port: int,
    mountpoint: str,
    user: str,
    password: str = "none",
    retry_s: float = 10.0,
) -> None:
    """Pull a live RTCM3 stream from an NTRIP caster into the router.

    NTRIP v1/v2 is an HTTP-style GET whose response body is the raw correction
    stream, so there is nothing to parse at this layer — the framing is done by
    `StreamFramer`, the same one the uplink uses, which validates CRC-24Q via
    pyrtcm and only ever emits whole frames. Reusing it is the point: the frames
    handed to the device are validated by exactly the same code that validates
    the frames arriving from a base.
    """
    cred = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = (
        f"GET /{mountpoint} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Ntrip-Version: Ntrip/2.0\r\n"
        f"User-Agent: NTRIP wormhole/1.0\r\n"
        f"Authorization: Basic {cred}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()

    while True:
        writer = None
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(request)
            await writer.drain()

            # Read only the response header, byte by byte, so no stream data is
            # swallowed with it.
            head = b""
            while b"\r\n\r\n" not in head and b"ICY 200 OK\r\n" not in head:
                b = await reader.read(1)
                if not b:
                    raise ConnectionError(f"caster closed during handshake: {head!r}")
                head += b
                if len(head) > 4096:
                    raise ConnectionError("caster header too long / not an NTRIP caster")

            first = head.split(b"\r\n", 1)[0].decode(errors="replace")
            if "200" not in first:
                raise ConnectionError(f"caster refused: {first!r}")

            logger.info("NTRIP source connected: %s:%d/%s", host, port, mountpoint)

            framer = StreamFramer()
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    raise ConnectionError("caster closed the stream")
                for frame in framer.feed(chunk):
                    if frame.kind == KIND_RTCM3:
                        router.publish(frame.raw)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a community caster comes and goes
            logger.warning("NTRIP source %s/%s: %s - retrying in %.0f s",
                           host, mountpoint, exc, retry_s)
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.sleep(retry_s)
