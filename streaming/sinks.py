"""Output sinks for the streaming server.

A `Sink` receives demuxed data. Today only file-based sinks are registered; the
NTRIP caster (RTCM3 -> caster) and a live-UBX fan-out can later be added as extra
sinks without touching the framer or the station logic.

File layout (per station, under STREAM_DIR):

    <stationId>/ubx/     <station>_ubx_YYYYMMDD_HH.ubx      hourly, GPS-time hour
                rtcm3/   <station>_rtcm3_YYYYMMDD_HH.rtcm3  hourly
                sensors/ <station>_<stream>_YYYYMMDD.csv    daily, append
                raw/     <station>_raw_YYYYMMDD_HH.bin      hourly, transport mitschnitt
                cli/     <station>_cli_YYYYMMDD.log

Rotation is keyed on the station's GPS clock. Because the binary writers are only
ever handed *whole frames*, a rotation can never split a UBX/RTCM3 message across
two files — which would leave the message unparseable in both.

When no GNSS fix has been seen yet the server clock is used instead and the file
name is suffixed `_sysclk`, so post-processing can never mistake such a file for
GNSS-timed data.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

from .frames import SENSOR_SPECS, SensorReading
from .gpstime import GnssClock, rtc_unix_to_datetime

CSV_BASE_COLUMNS = ("rtc_unix", "gps_iso", "utc_iso", "leap_s")

# stream name -> value column names (mirrors SENSOR_SPECS)
_STREAM_VALUE_COLUMNS = {spec[0]: spec[2] for spec in SENSOR_SPECS.values()}


class Sink:
    """Base sink. Subclasses override only what they care about."""

    def on_ubx(self, raw: bytes, stamp: datetime, sysclk: bool) -> None:
        pass

    def on_rtcm3(self, raw: bytes, stamp: datetime, sysclk: bool) -> None:
        pass

    def on_sensor(self, reading: SensorReading, clock: GnssClock) -> None:
        pass

    def on_cli(self, line: str, stamp: datetime, sysclk: bool) -> None:
        pass

    def on_raw(self, data: bytes) -> None:
        """Raw transport bytes, before demux."""

    def close(self) -> None:
        pass


class _RotatingFile:
    """Append-only file that rotates when its name key changes."""

    def __init__(self, directory: Path, name_fmt: str, mode: str = "ab"):
        self._dir = directory
        self._name_fmt = name_fmt  # e.g. "{station}_ubx_{key}.ubx"
        self._mode = mode
        self._key: str | None = None
        self._fh = None
        self.path: Path | None = None

    def _open(self, key: str, station: str) -> None:
        self.close()
        self._dir.mkdir(parents=True, exist_ok=True)
        self.path = self._dir / self._name_fmt.format(station=station, key=key)
        is_new = not self.path.exists()
        self._fh = open(self.path, self._mode)
        self._key = key
        self._on_open(is_new)

    def _on_open(self, is_new: bool) -> None:
        pass

    def _ensure(self, key: str, station: str) -> None:
        if key != self._key:
            self._open(key, station)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._key = None


class _BinaryStream(_RotatingFile):
    def write(self, data: bytes, key: str, station: str) -> None:
        self._ensure(key, station)
        self._fh.write(data)
        self._fh.flush()


class _CsvStream(_RotatingFile):
    def __init__(self, directory: Path, name_fmt: str, columns: tuple):
        super().__init__(directory, name_fmt, mode="a")
        self._columns = columns
        self._writer = None

    def _on_open(self, is_new: bool) -> None:
        self._writer = csv.writer(self._fh, lineterminator="\n")
        if is_new:
            self._writer.writerow(self._columns)

    def write_row(self, row: list, key: str, station: str) -> None:
        self._ensure(key, station)
        self._writer.writerow(row)
        self._fh.flush()


def hour_key(stamp: datetime, sysclk: bool) -> str:
    return stamp.strftime("%Y%m%d_%H") + ("_sysclk" if sysclk else "")


def day_key(stamp: datetime, sysclk: bool) -> str:
    return stamp.strftime("%Y%m%d") + ("_sysclk" if sysclk else "")


class FileSink(Sink):
    """Writes .ubx / .rtcm3 / raw .bin / sensor CSVs / CLI log for one station."""

    def __init__(self, root: Path, station_id: int, raw_capture: bool = True):
        self.station = str(station_id)
        base = root / self.station
        self._raw_capture = raw_capture

        self._ubx = _BinaryStream(base / "ubx", "{station}_ubx_{key}.ubx")
        self._rtcm3 = _BinaryStream(base / "rtcm3", "{station}_rtcm3_{key}.rtcm3")
        self._raw = _BinaryStream(base / "raw", "{station}_raw_{key}.bin")
        self._cli = _CsvStream(
            base / "cli", "{station}_cli_{key}.log", ("timestamp", "direction", "text")
        )
        self._sensors: dict[str, _CsvStream] = {}
        self._sensor_dir = base / "sensors"

    def on_ubx(self, raw: bytes, stamp: datetime, sysclk: bool) -> None:
        self._ubx.write(raw, hour_key(stamp, sysclk), self.station)

    def on_rtcm3(self, raw: bytes, stamp: datetime, sysclk: bool) -> None:
        self._rtcm3.write(raw, hour_key(stamp, sysclk), self.station)

    def on_raw(self, data: bytes) -> None:
        # The raw capture is a *transport* recording: it answers "when did these
        # bytes reach the server", so it is keyed on the server clock — never on
        # the GPS clock, and never with a _sysclk suffix.
        #
        # This matters: the IDENT frame arrives before the first RXM-RAWX, so a
        # GPS-keyed raw file would put the start of every session into a separate
        # `_sysclk` file that sorts *after* the main one. replay.py would then
        # read the stream out of order and never see the IDENT.
        if self._raw_capture:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            self._raw.write(data, now.strftime("%Y%m%d_%H"), self.station)

    def on_cli(self, line: str, stamp: datetime, sysclk: bool) -> None:
        direction, _, text = line.partition("\t")
        self._cli.write_row(
            [stamp.isoformat(timespec="seconds"), direction, text],
            day_key(stamp, sysclk),
            self.station,
        )

    def on_sensor(self, reading: SensorReading, clock: GnssClock) -> None:
        stream = reading.stream
        value_cols = _STREAM_VALUE_COLUMNS.get(stream)
        if value_cols is None:
            return

        writer = self._sensors.get(stream)
        if writer is None:
            writer = _CsvStream(
                self._sensor_dir,
                "{station}_" + stream + "_{key}.csv",
                CSV_BASE_COLUMNS + value_cols,
            )
            self._sensors[stream] = writer

        # The device timestamp is authoritative for sensor rows (it is GPS time).
        gps_dt = rtc_unix_to_datetime(reading.rtc_unix)
        utc_dt = clock.utc_of(gps_dt)
        row = [
            reading.rtc_unix,
            gps_dt.isoformat(timespec="seconds"),
            utc_dt.isoformat(timespec="seconds") if utc_dt else "",
            clock.leap_s if clock.leap_s is not None else "",
        ]
        row += [reading.values.get(c, "") for c in value_cols]
        writer.write_row(row, day_key(gps_dt, sysclk=False), self.station)

    def close(self) -> None:
        for stream in (self._ubx, self._rtcm3, self._raw, self._cli):
            stream.close()
        for writer in self._sensors.values():
            writer.close()
        self._sensors.clear()
