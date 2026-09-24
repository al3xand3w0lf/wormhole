"""An auto-registered base router must survive a garbage collection.

The event loop keeps only a WEAK reference to a task. `_ensure_base_router()`
started `router.run()` with a bare `ensure_future()`, and `run()` parks on an
`asyncio.Event` that nothing else references - so the whole cycle was
unreachable. A collection destroyed it, and destroying it runs `run()`'s
`finally`, which cancels every rover sender on that base: the rovers silently
stopped receiving corrections.

It surfaced on 2026-09-17 only because one more test file shifted GC timing
enough for `test_rover_auto_e2e` to log "Task was destroyed but it is pending!".
This test forces the collection instead of waiting for luck.
"""

import asyncio
import gc

import pytest

from streaming import config, server
from streaming.station import StationRegistry

BASE = 1001
ROVER = 1010


@pytest.mark.asyncio
async def test_auto_registered_router_and_its_senders_survive_gc(monkeypatch):
    monkeypatch.setattr(config, "STREAM_ROVER_AUTO_BASE_STATIONS", {BASE})
    monkeypatch.setattr(server, "registry", StationRegistry(server._make_sinks))
    monkeypatch.setattr(server, "_base_routers", {})
    monkeypatch.setattr(server, "_router_tasks", {})

    server._ensure_base_router(BASE)
    router = server._base_routers[BASE]
    await asyncio.sleep(0)                 # let run() reach its park
    router.add_rover(ROVER)
    await asyncio.sleep(0)
    sender = router._tasks[ROVER]

    for _ in range(3):
        gc.collect()
        await asyncio.sleep(0.01)

    try:
        assert not sender.cancelled(), "a GC must not cancel the rover's sender"
        assert not sender.done()
        task = server._router_tasks[BASE]
        assert not task.done(), "the base router's run() must still be running"
    finally:
        server._router_tasks[BASE].cancel()
        await asyncio.sleep(0)
