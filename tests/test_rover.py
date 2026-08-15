"""RTK rover correction downlink (streaming/rover.py).

The things worth testing are the ones that fail silently in the field: the
envelope contract, the drop policy, the write lock, and the base announcement.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from streaming.frames import (
    ID_RTCM_DATA,
    ID_RTCM_INFO,
    PRIVATE_CLASS,
    RTCM3_MAX_FRAME,
    encode_rtcm_data,
    encode_rtcm_info,
    ubx_payload,
)
from streaming import rover
from streaming.framer import StreamFramer
from streaming.rover import RoverRouter, _message_type


def make_rtcm3(msg_type: int = 1005, payload_len: int = 20) -> bytes:
    """A syntactically valid RTCM3 frame. The CRC is not computed - nothing in
    the downlink path checks it, which is itself deliberate: the receiver does,
    and its verdict is what the whole feature is measured by."""
    body = bytearray(payload_len)
    body[0] = (msg_type >> 4) & 0xFF
    body[1] = (msg_type & 0x0F) << 4
    head = bytes([0xD3, (payload_len >> 8) & 0x03, payload_len & 0xFF])
    return head + bytes(body) + b"\x00\x00\x00"


# -- the envelope contract --------------------------------------------------

def test_one_frame_per_envelope_round_trips():
    raw = make_rtcm3(1077, 300)
    env = encode_rtcm_data(raw)

    frames = list(StreamFramer().feed(env))
    assert len(frames) == 1
    assert frames[0].cls_ == PRIVATE_CLASS
    assert frames[0].id_ == ID_RTCM_DATA
    # The payload must be the RTCM3 frame, byte for byte and nothing else.
    assert ubx_payload(frames[0].raw) == raw


def test_oversized_frame_is_refused_not_truncated():
    # Truncating would make the device reassemble half a frame and the receiver
    # report a CRC failure - a link fault to anyone reading the counters, when
    # it is really a sizing bug on this side.
    with pytest.raises(ValueError):
        encode_rtcm_data(b"\xd3" + b"\x00" * RTCM3_MAX_FRAME)
    with pytest.raises(ValueError):
        encode_rtcm_data(b"")


def test_rtcm_info_carries_the_base_id():
    frames = list(StreamFramer().feed(encode_rtcm_info(290)))
    assert frames[0].id_ == ID_RTCM_INFO
    payload = ubx_payload(frames[0].raw)
    assert payload[0] | (payload[1] << 8) == 290


# -- the drop policy --------------------------------------------------------

class _Registry:
    def __init__(self, sessions=None):
        self.sessions = sessions or {}

    def get(self, station_id):
        return self.sessions.get(station_id)


def test_queue_drops_the_oldest_when_full():
    """Freshness beats completeness: a correction we are late with is not
    'better than nothing', the receiver takes it and quietly degrades."""
    router = RoverRouter(_Registry(), {1001}, queue_len=3)
    for i in range(5):
        router.publish(bytes([i]))

    q = router._queues[1001]
    assert list(q) == [b"\x02", b"\x03", b"\x04"]   # the oldest two are gone
    assert router.dropped_full == 2
    assert router.frames_in == 5


def test_rover_source_sink_publishes_every_frame_into_the_router():
    """A base's RoverSourceSink is just publish() behind the Sink interface: every
    RTCM3 frame the base sends becomes one correction offered to its rovers."""
    router = RoverRouter(_Registry(), {1002}, queue_len=4)
    sink = rover.RoverSourceSink(router)

    for _ in range(3):
        sink.on_rtcm3(make_rtcm3(), datetime.now(timezone.utc), False)

    assert router.frames_in == 3
    assert len(router._queues[1002]) == 3


def test_offline_station_drops_rather_than_queues():
    router = RoverRouter(_Registry(), {1001}, queue_len=4)
    router.publish(make_rtcm3())

    async def drive():
        task = asyncio.ensure_future(router._station_sender(1001))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(drive())
    assert router.dropped_offline == 1
    assert router.frames_out == 0


# -- dynamic (un)subscription ------------------------------------------------

def test_add_rover_is_idempotent_and_starts_delivering():
    session = _Session()
    registry = _Registry({1002: session})
    router = RoverRouter(registry, {1001}, queue_len=4)

    async def drive():
        task = asyncio.ensure_future(router.run())
        await asyncio.sleep(0.02)

        assert router.add_rover(1002) is True
        assert router.add_rover(1002) is False  # already there
        assert 1002 in router._tasks

        router.publish(make_rtcm3())
        await asyncio.sleep(0.05)
        assert router.frames_out == 1
        assert 1002 in router.status()["rovers"]

        task.cancel()

    asyncio.run(drive())


def test_remove_rover_stops_delivery_and_cancels_the_sender():
    session = _Session()
    registry = _Registry({1001: session})
    router = RoverRouter(registry, {1001}, queue_len=4)

    async def drive():
        task = asyncio.ensure_future(router.run())
        await asyncio.sleep(0.02)
        assert 1001 in router._tasks

        assert router.remove_rover(1001) is True
        assert router.remove_rover(1001) is False  # already gone
        await asyncio.sleep(0.02)
        assert 1001 not in router._tasks
        assert 1001 not in router.status()["rovers"]

        # a correction published now must not be delivered or even queued
        router.publish(make_rtcm3())
        await asyncio.sleep(0.05)
        assert session.writer.data == b""
        assert router.frames_out == 0

        task.cancel()

    asyncio.run(drive())


def test_run_starts_with_zero_rovers_and_stays_alive():
    """A base with no rovers yet (auto-registered by role=base, or simply
    unconfigured) must still be a live task under server.py's gather() - see
    the run() docstring. If it returned immediately (the pre-2026-08-14
    behaviour), add_rover() would have no supervisor to have spawned from."""
    router = RoverRouter(_Registry(), set(), queue_len=4)

    async def drive():
        task = asyncio.ensure_future(router.run())
        await asyncio.sleep(0.05)
        assert not task.done()  # still running despite zero rovers

        router.add_rover(1001)
        await asyncio.sleep(0.02)
        assert 1001 in router._tasks

        sender_task = router._tasks[1001]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        assert sender_task.cancelled()  # run()'s shutdown cancels every sender too

    asyncio.run(drive())


def test_add_rover_works_without_run_having_been_called():
    """add_rover() only needs a running event loop, not router.run() itself
    having started - the admin subscribe endpoint calls it directly on a
    router whose run() task is already alive somewhere else in the process."""
    session = _Session()
    router = RoverRouter(_Registry({1001: session}), set(), queue_len=4)

    async def drive():
        router.add_rover(1001)
        router.publish(make_rtcm3())
        await asyncio.sleep(0.05)
        assert router.frames_out == 1

    asyncio.run(drive())


# -- the write lock ---------------------------------------------------------

class _Writer:
    def __init__(self, stuck=False):
        self.data = bytearray()
        self.closed = False
        self.stuck = stuck          # drain() that never returns, as in the field

    def write(self, b):
        self.data.extend(b)

    async def drain(self):
        if self.stuck:
            await asyncio.Event().wait()
        await asyncio.sleep(0)

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, peer="10.0.0.1:1234"):
        self.connected = True
        self.writer = _Writer()
        self.write_lock = asyncio.Lock()
        self.file_transfer_active = False
        self.transfer_in_progress = False
        self.peer = peer
        # A real StationSession stamps this fresh in bind(); the router uses it
        # as the connection's identity.
        self.connected_since = datetime.now(timezone.utc)

    def unbind(self, writer=None):
        self.writer = None
        self.connected = False


def test_send_takes_the_write_lock():
    """Without it, an envelope lands inside a file transfer's raw byte stream -
    the same splice hazard as the download direction, just reversed."""
    session = _Session()
    router = RoverRouter(_Registry({1001: session}), {1001}, queue_len=4)

    async def drive():
        await session.write_lock.acquire()          # pretend a transfer holds it
        task = asyncio.ensure_future(router._station_sender(1001))
        router.publish(make_rtcm3())
        await asyncio.sleep(0.05)
        assert session.writer.data == b""           # blocked, not spliced in
        session.write_lock.release()
        await asyncio.sleep(0.05)
        assert session.writer.data                  # delivered once free
        task.cancel()

    asyncio.run(drive())
    assert router.frames_out == 1


# -- the sender must never park forever -------------------------------------

def test_a_stuck_write_drops_the_connection_instead_of_parking_forever(monkeypatch):
    """The 2026-08-07 failure: the sender froze mid-write, slept through a
    disconnect *and* a reconnect, and left the downlink dead with no counter
    moving and nothing in the log. A bounded write plus dropping the socket is
    what turns a permanent outage into a few seconds of gap."""
    monkeypatch.setattr(rover, "WRITE_TIMEOUT_S", 0.05)

    session = _Session()
    session.writer = _Writer(stuck=True)
    registry = _Registry({1001: session})
    router = RoverRouter(registry, {1001}, queue_len=4)

    async def drive():
        task = asyncio.ensure_future(router._station_sender(1001))
        router.publish(make_rtcm3())
        await asyncio.sleep(0.3)

        assert session.writer is None, "the stuck connection must be dropped"
        assert router.dropped_offline == 1
        assert router.frames_out == 0

        # ...and the loop is alive: a fresh connection is served again
        healthy = _Session(peer="10.0.0.9:9999")
        registry.sessions[1001] = healthy
        router.publish(make_rtcm3())
        await asyncio.sleep(0.1)
        assert router.frames_out == 1

        task.cancel()

    asyncio.run(drive())


def test_an_unencodable_frame_does_not_kill_the_sender():
    """encode_rtcm_data() refuses an oversized frame. That refusal used to escape
    the task, which killed the downlink for good - the same silent-death shape as
    the stuck write."""
    session = _Session()
    router = RoverRouter(_Registry({1001: session}), {1001}, queue_len=4)

    async def drive():
        task = asyncio.ensure_future(router._station_sender(1001))
        router.publish(make_rtcm3(1077, RTCM3_MAX_FRAME + 10))   # refused
        router.publish(make_rtcm3())                             # must still go out
        await asyncio.sleep(0.1)
        task.cancel()

    asyncio.run(drive())
    assert router.frames_out == 1
    assert router.dropped_offline == 1


# -- the base announcement --------------------------------------------------

def make_rtcm3_1005(station_id: int = 290) -> bytes:
    """A *real* 1005 - correct CRC and a readable DF003, so pyrtcm parses it.

    make_rtcm3() above deliberately leaves the CRC wrong, which is fine for the
    envelope tests but makes _reference_station_id() return None, and then the
    announcement path is never entered at all.
    """
    from pyrtcm.rtcmhelpers import calc_crc24q

    bits = f"{1005:012b}" + f"{station_id:012b}"
    payload = int(bits.ljust(19 * 8, "0"), 2).to_bytes(19, "big")
    body = bytes([0xD3, 0x00, 19]) + payload
    return body + calc_crc24q(body).to_bytes(3, "big")


def _info_frames(data: bytes) -> list:
    return [f for f in StreamFramer().feed(bytes(data))
            if f.cls_ == PRIVATE_CLASS and f.id_ == ID_RTCM_INFO]


def test_base_is_announced_once_per_connection_not_once_per_base_change():
    """A rover forgets its base across a reboot, so every new session needs the
    RTCM_INFO again - even though the base id never changed. Without this the
    device receives a perfectly good correction stream and reports base `?0`."""
    registry = _Registry()
    router = RoverRouter(registry, {1001}, queue_len=4)

    async def drive():
        task = asyncio.ensure_future(router._station_sender(1001))

        first = _Session(peer="10.0.0.1:1111")
        registry.sessions[1001] = first
        router.publish(make_rtcm3_1005())
        await asyncio.sleep(0.05)
        assert len(_info_frames(first.writer.data)) == 1

        # same base, same connection: announced once, not on every frame
        router.publish(make_rtcm3_1005())
        await asyncio.sleep(0.05)
        assert len(_info_frames(first.writer.data)) == 1

        # the device reboots and comes back on a new socket
        second = _Session(peer="10.0.0.2:2222")
        registry.sessions[1001] = second
        router.publish(make_rtcm3_1005())
        await asyncio.sleep(0.05)
        assert len(_info_frames(second.writer.data)) == 1

        task.cancel()

    asyncio.run(drive())


def test_the_base_announcement_is_repeated_on_an_interval(monkeypatch):
    """The device's copy of the base can vanish without the server learning of
    it - a reboot, a `rover zero`, or that one envelope discarded under the
    device's backlog limit. Seen live on 2026-08-07: `net: baseId=?0` on a
    station receiving a clean stream. Once per connection cannot recover."""
    monkeypatch.setattr(rover, "RTCM_INFO_INTERVAL_S", 0.1)

    session = _Session()
    router = RoverRouter(_Registry({1001: session}), {1001}, queue_len=4)

    async def drive():
        task = asyncio.ensure_future(router._station_sender(1001))

        router.publish(make_rtcm3_1005())
        await asyncio.sleep(0.05)
        assert len(_info_frames(session.writer.data)) == 1

        # inside the interval: no repeat
        router.publish(make_rtcm3_1005())
        await asyncio.sleep(0.02)
        assert len(_info_frames(session.writer.data)) == 1

        # past it: announced again, same connection and same base
        await asyncio.sleep(0.12)
        router.publish(make_rtcm3_1005())
        await asyncio.sleep(0.05)
        assert len(_info_frames(session.writer.data)) == 2

        task.cancel()

    asyncio.run(drive())


def test_file_transfer_drops_instead_of_queueing_behind():
    """Queueing behind a transfer would deliver a burst of stale corrections the
    moment it finished, which is worse than the gap."""
    session = _Session()
    session.file_transfer_active = True
    router = RoverRouter(_Registry({1001: session}), {1001}, queue_len=4)

    async def drive():
        task = asyncio.ensure_future(router._station_sender(1001))
        router.publish(make_rtcm3())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(drive())
    assert session.writer.data == b""
    assert router.dropped_offline == 1


# -- the type filter --------------------------------------------------------

def test_filter_drops_before_the_queue_not_after():
    """The whole point of filtering in publish(): _queues is bounded, so ballast
    that is discarded at the far end has already evicted a real correction on the
    way through. Filtered frames must never occupy a queue slot."""
    router = RoverRouter(_Registry(), {1001}, queue_len=2, rtcm_types={1005, 1077})

    router.publish(make_rtcm3(1137))    # NavIC: the receiver reports "not used"
    router.publish(make_rtcm3(1127))
    router.publish(make_rtcm3(1005))
    router.publish(make_rtcm3(1077))

    assert [_message_type(f) for f in router._queues[1001]] == [1005, 1077]
    assert router.dropped_filtered == 2
    assert router.dropped_full == 0     # the ballast never took a slot
    assert router.frames_in == 4        # ...but it is still counted as delivered


def test_types_in_still_counts_what_the_caster_delivers():
    """types_in is the "what arrives" half of the pair with types_out. Filtering
    must not blind it, or the counters can no longer show what was dropped."""
    router = RoverRouter(_Registry(), {1001}, queue_len=4, rtcm_types={1005})
    router.publish(make_rtcm3(1137))
    router.publish(make_rtcm3(1005))

    assert set(router.types_in.as_dict()) == {"1005", "1137"}
    assert router.status()["rtcm_types"] == [1005]


def test_no_filter_configured_forwards_everything():
    """Empty is the default and must stay a pass-through: a server with headroom
    has no reason to drop anything, and a silent filter would be invisible."""
    for types in (None, set()):
        router = RoverRouter(_Registry(), {1001}, queue_len=8, rtcm_types=types)
        for t in (1005, 1077, 1087, 1097, 1127, 1137):
            router.publish(make_rtcm3(t))
        assert len(router._queues[1001]) == 6
        assert router.dropped_filtered == 0
        assert router.status()["rtcm_types"] is None


def test_an_unreadable_frame_is_not_on_the_allowlist():
    """A frame whose type cannot be read is dropped rather than passed - it is
    not on any allowlist, and guessing would defeat the point of one."""
    router = RoverRouter(_Registry(), {1001}, queue_len=4, rtcm_types={1005})
    router.publish(b"\xd3\x00")

    assert not router._queues[1001]
    assert router.dropped_filtered == 1
