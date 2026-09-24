"""streaming/geo.py: WGS84<->ECEF and baseline distance.

The one bug this guards against is the unit trap the module docstring warns
about: NAV-PVT's height arrives from pyubx2 as raw millimetres, not metres.
"""

import math

from streaming.geo import ecef_distance, from_navpvt, wgs84_to_ecef

# A reference point:
# station 290's 1005 ARP, decoded straight off pyrtcm, metres.
_REF_ECEF = (4278387.4699, 635620.7099, 4672340.0400)
_REF_LLA = (47.400298, 8.450366, 459.4)  # lat, lon, height(m) - reported in the same doc


def test_wgs84_to_ecef_matches_the_reference_arp():
    """Round-trips the geodesy the other direction from the 2026-08-07 write-up
    (that doc went ECEF -> LLA; this goes LLA -> ECEF) - agreement confirms
    both are the same conversion, not two independently-plausible bugs."""
    x, y, z = wgs84_to_ecef(*_REF_LLA)
    assert ecef_distance((x, y, z), _REF_ECEF) < 1.0  # sub-metre: within the doc's rounding


def test_from_navpvt_divides_height_by_1000():
    """The trap: NAV-PVT height is raw millimetres. Passing it unconverted
    would silently place the point ~6371 km off - not a crash, a rover that
    never finds a base "in range"."""
    lat, lon, height_m = _REF_LLA
    height_mm = round(height_m * 1000)

    from_mm = from_navpvt(lat, lon, height_mm)
    from_m = wgs84_to_ecef(lat, lon, height_m)
    assert ecef_distance(from_mm, from_m) < 0.01


def test_from_navpvt_unconverted_height_would_be_off_by_kilometres():
    """Negative control: confirms the trap is real, not theoretical - passing
    raw mm as if it were metres (the bug this file exists to prevent) lands
    thousands of km away, which is exactly why it would look like 'always out
    of range' rather than an exception."""
    lat, lon, height_m = _REF_LLA
    height_mm = round(height_m * 1000)

    correct = from_navpvt(lat, lon, height_mm)
    wrong = wgs84_to_ecef(lat, lon, height_mm)  # forgot the /1000
    assert ecef_distance(correct, wrong) > 100_000  # kilometres, not metres


def test_ecef_distance_is_symmetric_and_zero_for_identical_points():
    a = (4278387.0, 635620.0, 4672340.0)
    b = (4278390.0, 635623.0, 4672341.0)
    assert ecef_distance(a, a) == 0.0
    assert math.isclose(ecef_distance(a, b), ecef_distance(b, a))


def test_ecef_distance_known_offset():
    a = (0.0, 0.0, 0.0)
    b = (3.0, 4.0, 0.0)
    assert ecef_distance(a, b) == 5.0
