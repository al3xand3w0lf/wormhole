#!/usr/bin/env python3
"""Generate the bundled Millipede caster's config from .env, and write back a
mountpoint password for any station that does not have one yet.

Run by caster/setup.sh after the caster is built; safe to re-run (idempotent -
an existing STREAM_CASTER_PASSWORDS entry is kept, never regenerated, so a
re-run to add a station does not invalidate every other station's already-
provisioned credential). See caster/README.md.

Reads STREAM_CASTER_STATIONS (which stations to provision) and STREAM_CASTER_PORT
from .env; writes STREAM_CASTER_PASSWORDS and STREAM_CASTER_ENABLE=true back into
.env, and (over)writes caster.yaml / sourcetable.dat / source.auth / blocklist /
host.auth under caster/millipede-caster/etc/.

Also derives each station's real position from its own RTCM 1005/1006 ARP (read
from its most recent archived .rtcm3 file under STREAM_DIR) and writes a
Millipede "NEAR" virtual mountpoint once at least one station has one - see
docs/millipede-near-base-2026-08-28.md and the comments in _write_sourcetable().
A station with no archive yet just keeps the 0.00/0.00 placeholder; re-run this
script once it has pushed data to pick up its real position.
"""

import math
import secrets
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

CASTER_DIR = Path(__file__).resolve().parent
REPO_DIR = CASTER_DIR.parent
sys.path.insert(0, str(REPO_DIR))
from streaming import stationdir  # noqa: E402
ENV_FILE = REPO_DIR / ".env"
BUILD_DIR = CASTER_DIR / "millipede-caster"
ETC_DIR = BUILD_DIR / "etc"
LOG_DIR = BUILD_DIR / "var" / "log"

# The RTCM3 types a typical single-band-to-multi-constellation rover set needs.
# Only advertised in the sourcetable for humans/clients browsing it - has no
# effect on what NtripCasterSink actually forwards (that is whatever the
# demuxer decodes for the station, unfiltered).
_DEFAULT_MSG_TYPES = "1005,1077,1087,1097,1127,1230"

# WGS84 ellipsoid constants - same as streaming/geo.py, duplicated rather than
# imported: geo.py deliberately only goes geodetic->ECEF (RoverAutoDiscovery
# never needs the inverse - see its docstring), and this is a second, one-off
# provisioning-time call site for the inverse, same reasoning tests/
# analyze_baseline.py already gave for not growing geo.py with it.
_WGS84_A = 6378137.0
_WGS84_F = 1 / 298.257223563
_WGS84_E2 = _WGS84_F * (2 - _WGS84_F)


def _parse_stations(value: str) -> list[int]:
    return sorted({int(s) for s in (p.strip() for p in value.replace(";", ",").split(",")) if s.isdigit()})


def _parse_passwords(value: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for pair in value.replace(";", ",").split(","):
        station, _, password = pair.partition(":")
        station, password = station.strip(), password.strip()
        if station.isdigit() and password:
            result[int(station)] = password
    return result


def _ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float]:
    """ECEF -> (lat_deg, lon_deg); height is dropped, Millipede's NEAR lookup
    is 2D. Iterative inverse of geo.wgs84_to_ecef() - 5 passes converge to
    sub-millimetre for any point near Earth's surface."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - _WGS84_E2))
    for _ in range(5):
        sin_lat = math.sin(lat)
        n = _WGS84_A / math.sqrt(1 - _WGS84_E2 * sin_lat * sin_lat)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - _WGS84_E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon)


def _decode_station_position(station: int, stream_dir: Path) -> tuple[float, float] | None:
    """Real (lat_deg, lon_deg) from the station's own RTCM 1005/1006 ARP, read
    from its most recent archived .rtcm3 file - needed only for Millipede's
    NEAR lookup in _write_sourcetable(). A station that has never pushed a
    frame yet (no archive on disk) simply keeps the 0.00/0.00 placeholder, same
    as before this existed. Same pyrtcm decode as rover_discovery.py's
    BaseArpSink._decode_arp() / tests/analyze_baseline.py's decode_base_arp(),
    run here over the archive instead of a live socket.
    """
    # The archive directory may be labelled with the station name
    # ("A001_2001"), so ask stationdir instead of assuming the bare id - and
    # glob on the suffix, since the file names carry the same label.
    rtcm_dir = stationdir.resolve(stream_dir, station) / "rtcm3"
    if not rtcm_dir.is_dir():
        return None
    files = sorted(rtcm_dir.glob("*_rtcm3_*.rtcm3"))
    if not files:
        return None

    from pyrtcm import RTCMReader

    with open(files[-1], "rb") as f:
        for _raw, parsed in RTCMReader(f, quitonerror=0):
            if parsed is None:
                continue
            dx = getattr(parsed, "DF025", None)
            dy = getattr(parsed, "DF026", None)
            dz = getattr(parsed, "DF027", None)
            if dx is not None and dy is not None and dz is not None:
                return _ecef_to_geodetic(float(dx), float(dy), float(dz))
    return None


def main() -> None:
    if not ENV_FILE.exists():
        sys.exit(f"{ENV_FILE} not found - copy .env.example to .env and set "
                  "STREAM_CASTER_STATIONS first")

    env = dotenv_values(ENV_FILE)
    stations = _parse_stations(env.get("STREAM_CASTER_STATIONS", ""))
    if not stations:
        sys.exit("STREAM_CASTER_STATIONS is empty in .env - set it to the "
                  "station id(s) that should get a mountpoint, e.g. "
                  "STREAM_CASTER_STATIONS=1001")
    port = int(env.get("STREAM_CASTER_PORT", "2101") or "2101")

    passwords = _parse_passwords(env.get("STREAM_CASTER_PASSWORDS", ""))
    for station in stations:
        if station not in passwords:
            passwords[station] = secrets.token_urlsafe(18)

    stream_dir = Path(env.get("STREAM_DIR", "./data/incoming_stream"))
    if not stream_dir.is_absolute():
        stream_dir = REPO_DIR / stream_dir
    positions: dict[int, tuple[float, float]] = {}
    for station in stations:
        pos = _decode_station_position(station, stream_dir)
        if pos is not None:
            positions[station] = pos
        else:
            print(f"  station {station}: no archived RTCM 1005/1006 ARP yet - "
                  "keeping the 0.00/0.00 sourcetable placeholder for it "
                  "(re-run this script once it has pushed data)")

    ETC_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    _write_caster_yaml(port)
    _write_sourcetable(stations, positions)
    _write_source_auth(stations, passwords)
    (ETC_DIR / "blocklist").touch(exist_ok=True)
    (ETC_DIR / "host.auth").touch(exist_ok=True)  # only used by proxy mode, which we don't set

    passwords_value = ",".join(f"{s}:{p}" for s, p in sorted(passwords.items()))
    set_key(str(ENV_FILE), "STREAM_CASTER_PASSWORDS", passwords_value, quote_mode="never")
    set_key(str(ENV_FILE), "STREAM_CASTER_ENABLE", "true", quote_mode="never")

    print(f"Wrote {ETC_DIR} (stations: {stations})")
    print(f"Updated {ENV_FILE}: STREAM_CASTER_ENABLE=true, "
          f"STREAM_CASTER_PASSWORDS for {sorted(passwords)}")


def _write_caster_yaml(port: int) -> None:
    (ETC_DIR / "caster.yaml").write_text(f"""\
# Generated by caster/generate_config.py - re-run it to regenerate, don't
# hand-edit (it will be overwritten). See caster/README.md.
listen:
  - port: {port}
    ip:   0.0.0.0

host_auth_file:    {ETC_DIR / "host.auth"}
blocklist_file:    {ETC_DIR / "blocklist"}
source_auth_file:  {ETC_DIR / "source.auth"}
sourcetable_file:  {ETC_DIR / "sourcetable.dat"}

access_log:  {LOG_DIR / "access.log"}
log:         {LOG_DIR / "caster.log"}
log_level:   INFO
""")


def _write_sourcetable(stations: list[int], positions: dict[int, tuple[float, float]]) -> None:
    lines = []
    for station in stations:
        lat, lon = positions.get(station, (0.00, 0.00))
        # Field 12 (nmea/"virtual base" in Millipede's own reading of it) MUST
        # be 0 for a real station - see caster/README.md's "Mount Point Taken"
        # trap. Lat/lon feeds the NEAR entry below (Millipede's ntripsrv.c
        # picks whichever real STR line here is geographically closest to a
        # connecting client's GGA) - stays at the 0.00/0.00 placeholder until
        # _decode_station_position() finds this station's first archived ARP.
        lines.append(
            f"STR;{station};station-{station};RTCM3;{_DEFAULT_MSG_TYPES};2;"
            f"GPS+GLO+GAL+BDS;NONE;NONE;{lat:.5f};{lon:.5f};0;0;wormhole;none;N;N;1200;"
        )
    if positions:
        # A virtual "NEAR" mountpoint: a client that connects here and sends
        # GGA gets routed by Millipede itself to whichever real STR above is
        # closest - nothing else in this repo is involved (see caster/README.md
        # for the mechanism). Only emitted once >=1 station has a real
        # position: with every real entry still at 0.00/0.00, NEAR would
        # resolve to an arbitrary one, not a nearest one, which is worse than
        # not advertising it. With exactly one real position it is a harmless
        # no-op (nothing to be "nearer" than) that starts doing real work the
        # moment a second station gets its own decoded ARP.
        lines.append(
            f"STR;NEAR;NEAR;RTCM3;{_DEFAULT_MSG_TYPES};2;"
            f"GPS+GLO+GAL+BDS;NONE;NONE;0.00;0.00;1;0;wormhole;none;N;N;1200;"
        )
    (ETC_DIR / "sourcetable.dat").write_text("\n".join(lines) + "\n")


def _write_source_auth(stations: list[int], passwords: dict[int, str]) -> None:
    lines = [
        f"{station}:wormhole:{passwords[station]}"
        for station in stations
    ]
    path = ETC_DIR / "source.auth"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


if __name__ == "__main__":
    main()
