"""The bundled caster's sourcetable / source.auth: one renderer, two callers.

`caster/generate_config.py` writes these two files at setup time from
`STREAM_CASTER_STATIONS`; `caster_provision.py` rewrites them at runtime when a
station identifies as `role=base`. They must produce byte-identical output for
the same station set - otherwise a later `caster/setup.sh` run would silently
drop every mountpoint the server had provisioned, and the server's next rewrite
would drop whatever setup.sh had added. Hence one renderer here, imported by
both, rather than the same format written out in two places.

Only the two files that change per station live here. `caster.yaml`, the
blocklist and `host.auth` are written once at setup time and stay in
`generate_config.py` - the server never touches them.

Reload is SIGHUP, sent to the caster process found by its own config path in
/proc (Millipede reloads `sourcetable_file` and `source_auth_file` on SIGHUP -
`caster_reload_sourcetables()` / `caster_reload_auth()`, caster/caster.c). We
look the pid up rather than going through `systemctl --user reload`: the
streaming server may run as a system unit with no user session bus, and the
caster may have been started by hand. Same uid is all this needs.
"""

import logging
import math
import os
import signal
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("streaming")

CASTER_DIR = Path(__file__).resolve().parent.parent / "caster"
ETC_DIR = CASTER_DIR / "millipede-caster" / "etc"

# The RTCM3 types a typical single-band-to-multi-constellation rover set needs.
# Only advertised in the sourcetable for humans/clients browsing it - it has no
# effect on what NtripCasterSink actually forwards (that is whatever the
# demuxer decodes for the station, unfiltered).
DEFAULT_MSG_TYPES = "1005,1077,1087,1097,1127,1230"

# WGS84 ellipsoid constants - same as streaming/geo.py, duplicated rather than
# imported: geo.py deliberately only goes geodetic->ECEF (RoverAutoDiscovery
# never needs the inverse - see its docstring), and this is the one call site
# for the inverse, at provisioning rather than routing rates.
_WGS84_A = 6378137.0
_WGS84_F = 1 / 298.257223563
_WGS84_E2 = _WGS84_F * (2 - _WGS84_F)


@dataclass(frozen=True)
class Mountpoint:
    """One station's mountpoint on the bundled caster.

    `lat`/`lon` are None until the station's own RTCM 1005/1006 ARP has been
    decoded - either live (caster_provision.CasterArpSink) or out of the
    archive (generate_config.py). They only feed Millipede's NEAR lookup; the
    push itself works from the first byte with the placeholder still in place.
    """

    station_id: int
    password: str
    lat: float | None = None
    lon: float | None = None


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float]:
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


def render_sourcetable(mounts: list[Mountpoint]) -> str:
    lines = []
    for mount in sorted(mounts, key=lambda m: m.station_id):
        lat = mount.lat if mount.lat is not None else 0.00
        lon = mount.lon if mount.lon is not None else 0.00
        # Field 12 (nmea/"virtual base" in Millipede's own reading of it) MUST
        # be 0 for a real station - see caster/README.md's "Mount Point Taken"
        # trap. Lat/lon feeds the NEAR entry below (Millipede's ntripsrv.c
        # picks whichever real STR line here is geographically closest to a
        # connecting client's GGA) - stays at the 0.00/0.00 placeholder until
        # this station's first ARP is decoded.
        lines.append(
            f"STR;{mount.station_id};station-{mount.station_id};RTCM3;{DEFAULT_MSG_TYPES};2;"
            f"GPS+GLO+GAL+BDS;NONE;NONE;{lat:.5f};{lon:.5f};0;0;wormhole;none;N;N;1200;"
        )
    if any(m.lat is not None for m in mounts):
        # A virtual "NEAR" mountpoint: a client that connects here and sends
        # GGA gets routed by Millipede itself to whichever real STR above is
        # closest - nothing else in this repo is involved (see
        # caster/README.md for the mechanism). Only emitted once >=1 station
        # has a real position: with every real entry still at 0.00/0.00, NEAR
        # would resolve to an arbitrary one, not a nearest one, which is worse
        # than not advertising it. With exactly one real position it is a
        # harmless no-op (nothing to be "nearer" than) that starts doing real
        # work the moment a second station gets its own decoded ARP.
        lines.append(
            f"STR;NEAR;NEAR;RTCM3;{DEFAULT_MSG_TYPES};2;"
            f"GPS+GLO+GAL+BDS;NONE;NONE;0.00;0.00;1;0;wormhole;none;N;N;1200;"
        )
    return "\n".join(lines) + "\n"


def render_source_auth(mounts: list[Mountpoint]) -> str:
    return "\n".join(
        f"{m.station_id}:wormhole:{m.password}"
        for m in sorted(mounts, key=lambda m: m.station_id)
    ) + "\n"


def read_positions(etc_dir: Path = ETC_DIR) -> dict[int, tuple[float, float]]:
    """Positions already in a generated sourcetable, so a rewrite keeps them.

    Without this, the first runtime rewrite would reset every station that had
    a real position back to the 0.00/0.00 placeholder (and drop the NEAR entry
    with it) until each one's ARP happens to come round again - a live
    regression caused purely by writing the file.
    """
    path = etc_dir / "sourcetable.dat"
    positions: dict[int, tuple[float, float]] = {}
    try:
        text = path.read_text()
    except OSError:
        return positions
    for line in text.splitlines():
        fields = line.split(";")
        if len(fields) < 11 or fields[0] != "STR" or not fields[1].isdigit():
            continue  # the NEAR entry and anything hand-added are not stations
        try:
            lat, lon = float(fields[9]), float(fields[10])
        except ValueError:
            continue
        if lat == 0.0 and lon == 0.0:
            continue  # the placeholder, not a position
        positions[int(fields[1])] = (lat, lon)
    return positions


def _write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file + rename, because the caster may read these at any
    moment: a SIGHUP we did not send (an operator's `systemctl reload`, or the
    other file's) must never find a half-written sourcetable."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)


def write_config(mounts: list[Mountpoint], etc_dir: Path = ETC_DIR) -> None:
    etc_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(etc_dir / "sourcetable.dat", render_sourcetable(mounts))
    _write_atomic(etc_dir / "source.auth", render_source_auth(mounts), mode=0o600)


def find_caster_pid(etc_dir: Path = ETC_DIR) -> int | None:
    """The pid of the caster running *this* config, by its own command line.

    Resolved against each candidate's own cwd rather than matched as a string:
    the systemd unit passes an absolute path, but a caster started by hand from
    the repo ("caster/... -c ./caster/.../caster.yaml") carries a relative one,
    and a substring match would then find nothing and silently skip every
    reload. Several casters commonly run on one host (see docs/), so the
    comparison has to be the resolved path, not "some process mentioning a
    caster.yaml".
    """
    target = os.path.realpath(etc_dir / "caster.yaml")
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            if not any(a.endswith(b"caster.yaml") for a in args):
                continue
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue  # exited between iterdir() and the read, or another uid
        for arg in args:
            if not arg.endswith(b"caster.yaml"):
                continue
            path = arg.decode("utf-8", "replace")
            if os.path.realpath(os.path.join(cwd, path)) == target:
                return int(entry.name)
    return None


def reload_caster(etc_dir: Path = ETC_DIR) -> bool:
    """SIGHUP the bundled caster so it re-reads sourcetable + source.auth.

    False (with a warning) rather than an exception on every failure mode: a
    caster that is not running, or runs as another uid, is a reason to tell the
    operator to reload it by hand - never a reason to fail the connection that
    triggered the provisioning.
    """
    pid = find_caster_pid(etc_dir)
    if pid is None:
        logger.warning("caster reload: no caster process found for %s - "
                       "reload it by hand once it runs (systemctl --user reload "
                       "millipede-caster)", etc_dir / "caster.yaml")
        return False
    try:
        os.kill(pid, signal.SIGHUP)
    except (ProcessLookupError, PermissionError) as exc:
        logger.warning("caster reload: cannot SIGHUP pid %d: %s - reload it by hand", pid, exc)
        return False
    logger.info("caster reload: SIGHUP sent to pid %d", pid)
    return True
