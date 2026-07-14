"""End-to-end: a real TCP client speaking the device's wire format."""

import asyncio

import pytest

from streaming import config, server
from streaming.frames import ID_SENSOR_INA219
from streaming.gpstime import gps_to_datetime
from streaming.sinks import FileSink
from streaming.station import StationRegistry

from .helpers import ident, rawx, rtcm3, sensor

WEEK = 2378
TOW = 3 * 86400 + 14 * 3600 + 12.0
STATION = 1001


@pytest.mark.asyncio
async def test_stream_is_demuxed_to_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STREAM_DIR", tmp_path)
    monkeypatch.setattr(
        server, "registry", StationRegistry(lambda sid: [FileSink(tmp_path, sid)])
    )

    srv = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    ubx_msg = rawx(WEEK, TOW)
    rtcm_msg = rtcm3(b"\x3e\xd0" + b"\x42" * 20)
    sensor_msg = sensor(ID_SENSOR_INA219, "<Iiii", 1_783_000_000, 12345, -678, 9012)

    _, writer = await asyncio.open_connection("127.0.0.1", port)
    # IDENT first, then a stream with a garbage run in the middle (a damaged stream).
    writer.write(ident(STATION) + ubx_msg + b"\x00\xff garbage \x11" + rtcm_msg + sensor_msg)
    await writer.drain()
    writer.close()
    await writer.wait_closed()

    for _ in range(50):  # let the server drain the connection
        await asyncio.sleep(0.02)
        if (tmp_path / str(STATION) / "rtcm3").exists():
            break

    srv.close()
    await srv.wait_closed()

    session = server.registry.get(STATION)
    assert session is not None
    assert session.ubx_frames == 1
    assert session.rtcm3_frames == 1
    assert session.private_frames == 1  # the sensor frame (IDENT is consumed separately)
    assert session.garbage_bytes > 0
    assert session.clock.valid
    assert session.clock.leap_s == 18

    server.registry.close_all()

    hour = gps_to_datetime(WEEK, TOW).strftime("%Y%m%d_%H")
    base = tmp_path / str(STATION)

    assert (base / "ubx" / f"{STATION}_ubx_{hour}.ubx").read_bytes() == ubx_msg
    assert (base / "rtcm3" / f"{STATION}_rtcm3_{hour}.rtcm3").read_bytes() == rtcm_msg

    csvs = list((base / "sensors").iterdir())
    assert len(csvs) == 1
    assert "12345,-678,9012" in csvs[0].read_text()

    # The raw capture must be a byte-exact recording of everything we sent, in
    # ONE file that begins with the IDENT.
    #
    # Regression guard: the raw capture is keyed on the *server* clock, not the GPS
    # clock. IDENT always arrives before the first RXM-RAWX, so a GPS-keyed raw file
    # would split the start of every session into a `_sysclk` file that sorts *after*
    # the main one — and replay.py, reading in name order, would never see the IDENT.
    raws = list((base / "raw").iterdir())
    assert len(raws) == 1
    assert "_sysclk" not in raws[0].name
    captured = raws[0].read_bytes()
    assert captured.startswith(ident(STATION))
    assert ubx_msg in captured
    assert rtcm_msg in captured


@pytest.mark.asyncio
async def test_raw_capture_replays_end_to_end(tmp_path, monkeypatch):
    """A recorded capture must round-trip through replay.py's pipeline."""
    monkeypatch.setattr(config, "STREAM_DIR", tmp_path)
    monkeypatch.setattr(
        server, "registry", StationRegistry(lambda sid: [FileSink(tmp_path, sid)])
    )

    srv = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    _, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(ident(STATION) + rawx(WEEK, TOW) + rtcm3(b"\x42" * 16))
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.3)

    srv.close()
    await srv.wait_closed()
    server.registry.close_all()

    # Replay the capture through the same framer/router/sinks into a fresh tree.
    from streaming.framer import StreamFramer
    from streaming.pipeline import ident_station_id, is_ident, route_frame
    from streaming.station import StationSession

    capture = next((tmp_path / str(STATION) / "raw").iterdir()).read_bytes()
    out = tmp_path / "replayed"
    framer = StreamFramer()
    session = None
    for frame in framer.feed(capture):
        if session is None:
            if is_ident(frame):
                sid = ident_station_id(frame)
                session = StationSession(sid, [FileSink(out, sid, raw_capture=False)])
            continue
        route_frame(session, frame)

    assert session is not None, "replay must find the IDENT"
    assert session.station_id == STATION
    assert session.ubx_frames == 1
    assert session.rtcm3_frames == 1
    session.close()

    hour = gps_to_datetime(WEEK, TOW).strftime("%Y%m%d_%H")
    assert (out / str(STATION) / "ubx" / f"{STATION}_ubx_{hour}.ubx").exists()


@pytest.mark.asyncio
async def test_connection_without_ident_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STREAM_DIR", tmp_path)
    monkeypatch.setattr(config, "STREAM_PRE_IDENT_CAP", 64)
    monkeypatch.setattr(
        server, "registry", StationRegistry(lambda sid: [FileSink(tmp_path, sid)])
    )

    srv = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    _, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"\x00" * 256)  # never identifies itself
    await writer.drain()
    await asyncio.sleep(0.2)

    assert server.registry.all() == []

    writer.close()
    srv.close()
    await srv.wait_closed()
