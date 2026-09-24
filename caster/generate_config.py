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
streaming/caster_config.py. A station
with no archive yet just keeps the 0.00/0.00 placeholder; re-run this script
once it has pushed data to pick up its real position. With
STREAM_CASTER_AUTO_ENABLE the running server fills that in live instead, off the
station's IDENT and first 1005 - see streaming/caster_provision.py.

sourcetable.dat and source.auth are rendered by streaming/caster_config.py, not
here: the running server writes the same two files when it auto-provisions a
base, and two renderers would mean each caller silently undoing the other's
stations on its next write.
"""

import secrets
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

CASTER_DIR = Path(__file__).resolve().parent
REPO_DIR = CASTER_DIR.parent
sys.path.insert(0, str(REPO_DIR))
from streaming import caster_config, stationdir  # noqa: E402
ENV_FILE = REPO_DIR / ".env"
BUILD_DIR = CASTER_DIR / "millipede-caster"
ETC_DIR = BUILD_DIR / "etc"
LOG_DIR = BUILD_DIR / "var" / "log"


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
                return caster_config.ecef_to_geodetic(float(dx), float(dy), float(dz))
    return None


def main() -> None:
    if not ENV_FILE.exists():
        sys.exit(f"{ENV_FILE} not found - copy .env.example to .env and set "
                  "STREAM_CASTER_STATIONS first")

    env = dotenv_values(ENV_FILE)
    stations = _parse_stations(env.get("STREAM_CASTER_STATIONS", ""))
    # An empty list is a valid starting point when the server provisions bases
    # itself: the caster then starts with an empty sourcetable and the first
    # station identifying as role=base fills it in. Without auto-provisioning
    # an empty caster would stay empty forever, so that is still an error.
    auto = (env.get("STREAM_CASTER_AUTO_ENABLE") or "").strip().lower() in ("1", "true", "yes", "on")
    if not stations and not auto:
        sys.exit("STREAM_CASTER_STATIONS is empty in .env - set it to the "
                  "station id(s) that should get a mountpoint, e.g. "
                  "STREAM_CASTER_STATIONS=1001 (or set STREAM_CASTER_AUTO_ENABLE=true "
                  "to start empty and let bases provision themselves)")
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
    # Positions already in the sourcetable are kept when this run could not
    # decode one: with the server auto-provisioning, it may well have written a
    # position from a live ARP that no archived file carries yet, and a setup
    # re-run must not throw that away.
    for station, pos in caster_config.read_positions(ETC_DIR).items():
        positions.setdefault(station, pos)
    caster_config.write_config([
        caster_config.Mountpoint(
            station_id=station,
            password=passwords[station],
            lat=positions.get(station, (None, None))[0],
            lon=positions.get(station, (None, None))[1],
        )
        for station in stations
    ], ETC_DIR)
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


if __name__ == "__main__":
    main()
