"""File layout, hourly rotation on the GPS clock, and sensor CSVs."""

from streaming.framer import StreamFramer
from streaming.gpstime import gps_to_datetime
from streaming.pipeline import route_frame
from streaming.sinks import FileSink
from streaming.station import StationSession

from .helpers import ident, rawx, rtcm3, sensor
from streaming.frames import ID_SENSOR_INA219, ID_SENSOR_SHT4X

WEEK = 2378
# Seconds-of-week landing on Wednesday 13:59:59 and 14:00:00 GPS time.
TOW_H13 = 3 * 86400 + 13 * 3600 + 3599.0
TOW_H14 = 3 * 86400 + 14 * 3600 + 0.0


def feed(session: StationSession, data: bytes, framer: StreamFramer) -> None:
    for frame in framer.feed(data):
        route_frame(session, frame)


def make_session(tmp_path, station_id=1001) -> StationSession:
    return StationSession(station_id, [FileSink(tmp_path, station_id)])


def test_ubx_rotates_on_gps_hour_change(tmp_path):
    session = make_session(tmp_path)
    framer = StreamFramer()

    feed(session, rawx(WEEK, TOW_H13), framer)
    feed(session, rawx(WEEK, TOW_H14), framer)
    session.close()

    h13 = gps_to_datetime(WEEK, TOW_H13).strftime("%Y%m%d_%H")
    h14 = gps_to_datetime(WEEK, TOW_H14).strftime("%Y%m%d_%H")
    assert h13 != h14

    ubx_dir = tmp_path / "1001" / "ubx"
    names = sorted(p.name for p in ubx_dir.iterdir())
    assert names == [f"1001_ubx_{h13}.ubx", f"1001_ubx_{h14}.ubx"]


def test_frames_are_never_split_across_files(tmp_path):
    """Each file must contain whole frames only - a split message would be
    unparseable in both halves."""
    session = make_session(tmp_path)
    framer = StreamFramer()

    msg13 = rawx(WEEK, TOW_H13)
    msg14 = rawx(WEEK, TOW_H14)
    feed(session, msg13 + msg14, framer)
    session.close()

    h13 = gps_to_datetime(WEEK, TOW_H13).strftime("%Y%m%d_%H")
    h14 = gps_to_datetime(WEEK, TOW_H14).strftime("%Y%m%d_%H")
    ubx_dir = tmp_path / "1001" / "ubx"

    assert (ubx_dir / f"1001_ubx_{h13}.ubx").read_bytes() == msg13
    assert (ubx_dir / f"1001_ubx_{h14}.ubx").read_bytes() == msg14


def test_rtcm3_recorded_separately(tmp_path):
    session = make_session(tmp_path)
    framer = StreamFramer()
    msg = rtcm3(b"\x3e\xd0" + b"\x42" * 20)

    feed(session, rawx(WEEK, TOW_H13) + msg, framer)
    session.close()

    h13 = gps_to_datetime(WEEK, TOW_H13).strftime("%Y%m%d_%H")
    path = tmp_path / "1001" / "rtcm3" / f"1001_rtcm3_{h13}.rtcm3"
    assert path.read_bytes() == msg


def test_sysclk_suffix_before_gnss_fix(tmp_path):
    """Without a GPS clock the server clock is used - and the file says so."""
    session = make_session(tmp_path)
    framer = StreamFramer()

    # A UBX frame that is not RAWX -> no clock yet.
    from .helpers import ubx

    feed(session, ubx(0x01, 0x07, b"\x00" * 8), framer)
    session.close()

    files = list((tmp_path / "1001" / "ubx").iterdir())
    assert len(files) == 1
    assert files[0].name.endswith("_sysclk.ubx")


def test_sensor_csv_header_and_utc_column(tmp_path):
    session = make_session(tmp_path)
    framer = StreamFramer()

    # RAWX first so leapS is known -> the UTC column can be filled.
    feed(session, rawx(WEEK, TOW_H13, leap_s=18), framer)
    feed(session, sensor(ID_SENSOR_INA219, "<Iiii", 1_783_000_000, 12345, -678, 9012), framer)
    session.close()

    csvs = list((tmp_path / "1001" / "sensors").iterdir())
    assert len(csvs) == 1
    lines = csvs[0].read_text().strip().splitlines()

    assert lines[0] == "rtc_unix,gps_iso,utc_iso,leap_s,mV,mA,mW"
    cells = lines[1].split(",")
    assert cells[0] == "1783000000"
    assert cells[3] == "18"
    assert cells[4:] == ["12345", "-678", "9012"]

    # utc_iso must be exactly gps_iso - 18 s
    from datetime import datetime, timedelta

    gps = datetime.fromisoformat(cells[1])
    utc = datetime.fromisoformat(cells[2])
    assert gps - utc == timedelta(seconds=18)


def test_sensor_utc_blank_until_leap_known(tmp_path):
    session = make_session(tmp_path)
    framer = StreamFramer()

    feed(session, sensor(ID_SENSOR_SHT4X, "<Iii", 1_783_000_000, 21500, 45300), framer)
    session.close()

    csvs = list((tmp_path / "1001" / "sensors").iterdir())
    cells = csvs[0].read_text().strip().splitlines()[1].split(",")
    assert cells[2] == ""  # utc_iso
    assert cells[3] == ""  # leap_s


def test_streams_go_to_separate_csvs(tmp_path):
    session = make_session(tmp_path)
    framer = StreamFramer()

    feed(session, sensor(ID_SENSOR_INA219, "<Iiii", 1_783_000_000, 1, 2, 3), framer)
    feed(session, sensor(ID_SENSOR_SHT4X, "<Iii", 1_783_000_000, 21500, 45300), framer)
    session.close()

    names = sorted(p.name for p in (tmp_path / "1001" / "sensors").iterdir())
    assert any("_ina219_" in n for n in names)
    assert any("_sht4x_" in n for n in names)
    assert len(names) == 2


def test_rtcm3_message_types_are_counted(tmp_path):
    """The type histogram is how you confirm the base emits 1005 + MSM7 + 1230."""
    from streaming.pipeline import rtcm3_message_type

    session = make_session(tmp_path)
    framer = StreamFramer()

    def rtcm_of_type(msg_type: int) -> bytes:
        # DF002 = first 12 bits of the payload.
        payload = bytes([(msg_type >> 4) & 0xFF, (msg_type & 0x0F) << 4]) + b"\x00" * 14
        return rtcm3(payload)

    assert rtcm3_message_type(rtcm_of_type(1005)) == 1005
    assert rtcm3_message_type(rtcm_of_type(1077)) == 1077

    feed(session, rtcm_of_type(1005) + rtcm_of_type(1077) + rtcm_of_type(1077), framer)
    session.close()

    assert session.rtcm3_types == {1005: 1, 1077: 2}
    assert session.status()["rtcm3_types"] == {1005: 1, 1077: 2}


def test_ident_does_not_produce_a_ubx_file(tmp_path):
    """Private frames are ours - they must not pollute the .ubx recording."""
    session = make_session(tmp_path)
    framer = StreamFramer()

    feed(session, ident(1001), framer)
    session.close()

    assert not (tmp_path / "1001" / "ubx").exists()
