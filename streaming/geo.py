"""WGS84 geodetic <-> ECEF and baseline distance.

Used by rover_discovery.py to turn a base's 1005 ARP (already ECEF, straight off
pyrtcm's DF025/026/027) and a rover's UBX-NAV-PVT fix (geodetic lat/lon/height)
into one comparable distance: the baseline.

No pyproj: the closed-form lat/lon/height -> ECEF conversion is exact (not an
iteration - that direction only needs one) and is standard geodesy, not a
protocol detail worth a dependency. It mirrors the ECEF -> lat/lon iteration
used for the reverse direction elsewhere, just run the other way.

THE UNIT TRAP THIS FILE EXISTS TO AVOID
----------------------------------------
u-blox has more of these: the survey-in fields scale differently again (`meanAcc` 0.1 mm, `meanX/Y/Z` cm). NAV-PVT has
its own: pyubx2 scales `lat`/`lon` to degrees (its scale table carries 1e-07 for
both), but leaves `height`/`hMSL` **unscaled** - they arrive as raw millimetres
(u-blox int32). Passing that straight into the formula below without /1000
would place every rover roughly 6371 km from the coordinate origin, i.e.
`ecef_distance()` would return nonsense two decimal orders too large - and
because a WGS84 point 6371 km further out from the ellipsoid is not a NaN or a
crash, this would fail *silently*: every rover would look "out of range" of
every base, and the whole feature would just never subscribe anyone.
`from_navpvt()` below is the one place that division happens.
"""

import math

# WGS84 ellipsoid constants.
_A = 6378137.0                    # semi-major axis, metres
_F = 1 / 298.257223563            # flattening
_E2 = _F * (2 - _F)               # first eccentricity squared


def wgs84_to_ecef(lat_deg: float, lon_deg: float, height_m: float) -> tuple[float, float, float]:
    """Geodetic (lat, lon, height above the ellipsoid) -> ECEF (X, Y, Z), metres."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)

    n = _A / math.sqrt(1 - _E2 * sin_lat * sin_lat)

    x = (n + height_m) * cos_lat * math.cos(lon)
    y = (n + height_m) * cos_lat * math.sin(lon)
    z = (n * (1 - _E2) + height_m) * sin_lat
    return x, y, z


def from_navpvt(lat_deg: float, lon_deg: float, height_mm: float) -> tuple[float, float, float]:
    """UBX-NAV-PVT's lat/lon (pyubx2-scaled, degrees) + height (raw mm) -> ECEF.

    height, not hMSL: height above the ellipsoid is what the WGS84 formula
    above expects; hMSL is above a geoid model and is not usable here without
    a separate correction. See the module docstring for the /1000 this exists
    to make impossible to forget.
    """
    return wgs84_to_ecef(lat_deg, lon_deg, height_mm / 1000.0)


def ecef_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """Straight-line distance between two ECEF points, metres.

    This *is* the baseline: for the sub-100 km ranges RTK corrections are
    useful over, the chord and the geodesic differ by millimetres, so there is
    no reason to reach for anything more elaborate.
    """
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(a, b)))
