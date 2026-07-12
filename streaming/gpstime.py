"""GNSS time.

*** The reference device works in GPS time, NOT UTC. ***

Its firmware derives the calendar directly from the GPS epoch (JD 2444245.0 =
1980-01-06) using week + seconds-of-week, and never applies the leap-second offset
— even though `leapS` is present in RXM-RAWX. Consequently both the device's own
file timestamps *and* its GNSS-synchronised RTC (hence the `rtc_unix` field in the
sensor frames) run ~18 s ahead of UTC.

The server reproduces that convention deliberately, so that hour boundaries in the
recorded .ubx files line up with the device's own and the two are mergeable.
`leapS` is recorded alongside, so a true UTC timestamp can still be derived (the
sensor CSVs carry it as a separate column).

  ==> Do NOT use pyubx2's UTC helpers (itow2utc etc.) here: they apply UTC
      conventions and would silently diverge from the device by the leap seconds.
      pyubx2 supplies the raw RXM-RAWX fields; the conversion below is ours.

  ==> If YOUR device already corrects for leap seconds, this module is the single
      place to change (subtract `leapS` in `gps_to_datetime`). tests/test_gpstime.py
      pins the current behaviour, so it will tell you what you changed.
"""

import math
from datetime import datetime, timedelta

# JD 2444245.0 — the GPS epoch, as hard-coded in the firmware.
GPS_EPOCH = datetime(1980, 1, 6)

# The firmware validates the derived year against this range.
MIN_YEAR = 2020
MAX_YEAR = 2100

# A GNSS receiver may briefly report a stale/garbled epoch. Ignore a time that
# jumps backwards by more than this and keep the previous clock.
MAX_BACKSTEP = timedelta(seconds=30)


def gps_to_datetime(week: int, rcv_tow: float) -> datetime:
    """GPS week + seconds-of-week -> GPS-time calendar (naive datetime, NOT UTC).

    Equivalent to the firmware's Julian-Date arithmetic: because GPS time is a
    uniform timescale with no leap seconds, adding `week` weeks and `rcv_tow`
    seconds to the GPS epoch yields exactly the same calendar date/time that
    the device firmware computes. `tests/test_gpstime.py` cross-validates this
    against a literal port of that firmware algorithm.

    The 0.1 s rounding mirrors the firmware (`floor(tow * 10 + 0.5) / 10`).
    """
    sow = math.floor(rcv_tow * 10.0 + 0.5) / 10.0
    return GPS_EPOCH + timedelta(weeks=week, seconds=sow)


def rtc_unix_to_datetime(rtc_unix: int) -> datetime:
    """Decode the device's `rtc_unix` sensor timestamp.

    The device builds it from its GNSS-synced RTC, i.e. it is the *GPS* calendar
    encoded as if it were unix seconds. Decoding it the same way round-trips it.
    """
    return datetime(1970, 1, 1) + timedelta(seconds=int(rtc_unix))


class GnssClock:
    """Per-station GPS clock, fed from every valid UBX-RXM-RAWX frame."""

    def __init__(self):
        self.gps_time: datetime | None = None
        self.leap_s: int | None = None

    @property
    def valid(self) -> bool:
        return self.gps_time is not None

    def update_from_rawx(self, week: int, rcv_tow: float, leap_s: int) -> bool:
        """Update from RXM-RAWX fields. Returns True if the clock was accepted."""
        try:
            t = gps_to_datetime(week, rcv_tow)
        except (OverflowError, ValueError, OSError):
            return False

        if not (MIN_YEAR <= t.year <= MAX_YEAR):
            return False
        if self.gps_time is not None and (self.gps_time - t) > MAX_BACKSTEP:
            return False

        self.gps_time = t
        # leapS == 0 means the receiver has not decoded it yet — don't latch a zero.
        if leap_s:
            self.leap_s = int(leap_s)
        return True

    def utc_of(self, gps_dt: datetime) -> datetime | None:
        """Convert a GPS-time datetime to true UTC, if leapS is known yet."""
        if self.leap_s is None:
            return None
        return gps_dt - timedelta(seconds=self.leap_s)
