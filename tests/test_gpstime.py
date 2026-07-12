"""The GPS->calendar conversion must match the firmware bit for bit.

If it drifts, stream-recorded .ubx hour boundaries stop lining up with the
device-named files from batch mode, and the sensor timestamps go subtly wrong.
So we cross-validate our clean implementation against a *literal port* of
the GPS-time routine in the device firmware.
"""

import math
import random
from datetime import datetime, timedelta

import pytest

from streaming.gpstime import GnssClock, gps_to_datetime, rtc_unix_to_datetime


def firmware_gps_time(week: int, rcv_tow: float) -> datetime:
    """Literal port of the firmware routine (Julian-Date arithmetic, no leap seconds)."""
    sow = math.floor(rcv_tow * 10.0 + 0.5) / 10.0

    jd = 2444245.0 + week * 7.0 + math.floor(sow / 86400.0)

    t2 = math.floor((jd - 1867216.25) / 36524.25)
    t3 = jd + 1.0 + t2 - math.floor(t2 / 4.0) - 1720995.0
    year = int(math.floor((t3 - 122.1) / 365.25))
    t1 = math.floor(365.25 * year)
    month = int(math.floor((t3 - t1) / 30.6001))
    day = int(math.floor(t3 - t1 - math.floor(30.6001 * month)))

    if month > 13:
        month -= 13
    else:
        month -= 1
    if month <= 2:
        year += 1

    sod = sow - math.floor(sow / 86400.0) * 86400.0
    hours = int(math.floor(sod / 3600.0))
    minutes = int(math.floor((sod - hours * 3600.0) / 60.0))
    seconds = int(math.floor(sod - hours * 3600.0 - minutes * 60.0))

    return datetime(year, month, day, hours, minutes, seconds)


def test_gps_epoch_anchor():
    assert gps_to_datetime(0, 0.0) == datetime(1980, 1, 6)


EDGE_TOWS = [
    0.0,  # start of GPS week (Sunday 00:00)
    0.1,
    1.0,
    3599.9,  # just before an hour boundary
    3600.0,  # exactly on an hour boundary
    3600.1,
    86399.9,  # just before midnight
    86400.0,  # exactly midnight -> next day
    302400.0,  # mid-week
    604799.0,  # last second of the week
]


@pytest.mark.parametrize("week", [2100, 2200, 2250, 2300, 2350, 2400, 2500])
@pytest.mark.parametrize("tow", EDGE_TOWS)
def test_matches_firmware_on_edges(week, tow):
    ours = gps_to_datetime(week, tow).replace(microsecond=0)
    assert ours == firmware_gps_time(week, tow)


def test_matches_firmware_randomised():
    rng = random.Random(20260712)
    for _ in range(3000):
        week = rng.randint(2080, 2600)  # ~2019 .. ~2029
        tow = rng.uniform(0.0, 604800.0)
        ours = gps_to_datetime(week, tow).replace(microsecond=0)
        assert ours == firmware_gps_time(week, tow), f"week={week} tow={tow}"


def test_no_leap_second_correction_applied():
    """Sanity: the conversion is pure GPS time - leapS must not shift it."""
    a = gps_to_datetime(2378, 100000.0)
    b = gps_to_datetime(2378, 100000.0)
    assert a == b
    # 18 s of leap seconds would show up as an offset; assert it does not.
    assert (a - datetime(1980, 1, 6)) == timedelta(weeks=2378, seconds=100000.0)


def test_rtc_unix_roundtrip():
    dt = rtc_unix_to_datetime(1_783_000_000)
    assert dt == datetime(1970, 1, 1) + timedelta(seconds=1_783_000_000)


class TestGnssClock:
    def test_accepts_plausible_time(self):
        clock = GnssClock()
        assert clock.update_from_rawx(2378, 100000.0, 18) is True
        assert clock.valid
        assert clock.leap_s == 18

    def test_rejects_implausible_year(self):
        clock = GnssClock()
        assert clock.update_from_rawx(0, 0.0, 18) is False  # 1980
        assert not clock.valid

    def test_rejects_large_backstep(self):
        clock = GnssClock()
        clock.update_from_rawx(2378, 100000.0, 18)
        before = clock.gps_time
        assert clock.update_from_rawx(2378, 50000.0, 18) is False
        assert clock.gps_time == before

    def test_allows_small_jitter(self):
        clock = GnssClock()
        clock.update_from_rawx(2378, 100000.0, 18)
        assert clock.update_from_rawx(2378, 99990.0, 18) is True

    def test_does_not_latch_zero_leap(self):
        clock = GnssClock()
        clock.update_from_rawx(2378, 100000.0, 0)
        assert clock.leap_s is None
        clock.update_from_rawx(2378, 100001.0, 18)
        assert clock.leap_s == 18

    def test_utc_is_gps_minus_leap(self):
        clock = GnssClock()
        clock.update_from_rawx(2378, 100000.0, 18)
        gps = clock.gps_time
        assert clock.utc_of(gps) == gps - timedelta(seconds=18)

    def test_utc_unknown_before_leap_seen(self):
        clock = GnssClock()
        clock.update_from_rawx(2378, 100000.0, 0)
        assert clock.utc_of(clock.gps_time) is None
