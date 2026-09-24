"""Full stack: a role=base and a role=rover device, both real TCP clients,
prove out the whole chain server.py wires together - IDENT role decode,
dynamic base registration, ARP decode, NAV-PVT decode, nearest-base
subscription, and an actual correction landing in the rover's socket. Unit
coverage for each piece lives in test_rover.py / test_rover_discovery.py; this
is the one test that would catch a wiring mistake none of those could see.
"""

import asyncio

import pytest
from pyubx2 import GET, UBXMessage

from streaming import config, server
from streaming.frames import ID_RTCM_DATA, PRIVATE_CLASS, ROLE_BASE, ROLE_ROVER
from streaming.framer import StreamFramer
from streaming.rover_discovery import RoverAutoDiscovery
from streaming.station import StationRegistry

from .helpers import ident, rtcm3, rtcm3_1005

BASE = 1001
ROVER = 1010
# A reference point - base ARP and rover fix at (almost) the same spot,
# so the baseline is small and well inside any reasonable max-baseline.
BASE_ECEF = (4278387.4699, 635620.7099, 4672340.0400)
ROVER_LAT, ROVER_LON, ROVER_HEIGHT_MM = 47.400298, 8.450366, 459400


async def _read_rtcm_data_frames(reader: asyncio.StreamReader, timeout_s: float = 3.0) -> list:
    framer = StreamFramer()
    frames = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline and not frames:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=0.2)
        except asyncio.TimeoutError:
            continue
        if not chunk:
            break
        frames.extend(f for f in framer.feed(chunk)
                      if f.cls_ == PRIVATE_CLASS and f.id_ == ID_RTCM_DATA)
    return frames


@pytest.mark.asyncio
async def test_role_rover_auto_subscribes_to_role_base_and_receives_corrections(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "STREAM_DIR", tmp_path)
    monkeypatch.setattr(config, "STREAM_ROVER_AUTO_ENABLE", True)
    monkeypatch.setattr(config, "STREAM_ROVER_AUTO_BASE_STATIONS", {BASE})
    monkeypatch.setattr(config, "STREAM_ROVER_QUEUE", 8)
    monkeypatch.setattr(config, "STREAM_ROVER_RTCM_TYPES", set())

    # A fresh registry (still backed by the real _make_sinks) and fresh rover-
    # discovery state - handle_connection() is driven directly here, not
    # run(), so this test wires the same module globals run() normally would.
    monkeypatch.setattr(server, "registry", StationRegistry(server._make_sinks))
    monkeypatch.setattr(server, "_base_routers", {})
    monkeypatch.setattr(server, "_station_roles", {})
    monkeypatch.setattr(server, "_manual_bases", {})
    discovery = RoverAutoDiscovery(server._base_routers, set(), max_baseline_km=40.0,
                                   switch_margin_km=5.0)
    monkeypatch.setattr(server, "_discovery", discovery)

    srv = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    # -- the base connects, declares role=base, and its first 1005 gives it an ARP
    _, base_writer = await asyncio.open_connection("127.0.0.1", port)
    base_writer.write(ident(BASE, ROLE_BASE) + rtcm3_1005(290, *BASE_ECEF))
    await base_writer.drain()

    for _ in range(100):
        await asyncio.sleep(0.02)
        if BASE in server._base_routers and BASE in discovery.base_positions:
            break
    assert BASE in server._base_routers, "role=base IDENT must auto-register a router"
    assert BASE in discovery.base_positions, "the 1005 must decode into a base position"

    # -- the rover connects, declares role=rover, and a 3D fix should subscribe it
    rover_reader, rover_writer = await asyncio.open_connection("127.0.0.1", port)
    rover_writer.write(ident(ROVER, ROLE_ROVER))
    await rover_writer.drain()
    await asyncio.sleep(0.05)

    navpvt = UBXMessage("NAV", "NAV-PVT", GET, lat=ROVER_LAT, lon=ROVER_LON,
                        height=ROVER_HEIGHT_MM, fixType=3).serialize()
    rover_writer.write(navpvt)
    await rover_writer.drain()

    for _ in range(100):
        await asyncio.sleep(0.02)
        if ROVER in server._base_routers[BASE].station_ids:
            break
    assert ROVER in server._base_routers[BASE].station_ids
    assert discovery.rover_subscribed[ROVER] == BASE

    # -- a correction from the base must actually land in the rover's socket
    base_writer.write(rtcm3(b"\x43\x50" + b"\x00" * 18))
    await base_writer.drain()

    frames = await _read_rtcm_data_frames(rover_reader)
    assert frames, "the rover must receive at least one RTCM_DATA envelope"

    for w in (base_writer, rover_writer):
        w.close()
    srv.close()
    await srv.wait_closed()
    server.registry.close_all()
