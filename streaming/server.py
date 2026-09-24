"""TCP data plane (:9000) + HTTP admin/CLI control plane (:9001).

Both run in one asyncio event loop, in a process separate from the batch HTTP
server (server.py), which stays untouched.
"""

import asyncio
import logging
import signal
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

import uvicorn
from dotenv import dotenv_values
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from . import config
from .caster_provision import CasterArpSink, CasterAutoProvision
from .frames import ROLE_BASE, ROLE_ROVER
from .gnss_tunnel import registry as tunnel_registry
from .framer import StreamFramer
from .ntrip import NtripCasterSink
from .pipeline import decode_ident, is_ident, route_frame
from .rover import RoverRouter, RoverSourceSink, ntrip_source
from .rover_discovery import BaseArpSink, RoverAutoDiscovery, RoverPositionSink
from .sinks import FileSink
from .station import StationRegistry, is_download_class

logger = logging.getLogger("streaming")


def setup_logging() -> None:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handlers = [
        RotatingFileHandler(
            config.LOG_FILE,
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT,
        ),
        logging.StreamHandler(),  # journald
    ]
    for handler in handlers:
        handler.setFormatter(fmt)
        logger.addHandler(handler)


def sweep_stale_raw() -> None:
    """Drop raw captures older than STREAM_RAW_MAX_AGE_H (they are the bulk of the data)."""
    if config.STREAM_RAW_MAX_AGE_H <= 0:
        return
    cutoff = time.time() - config.STREAM_RAW_MAX_AGE_H * 3600
    removed = 0
    for path in config.STREAM_DIR.glob("*/raw/*.bin"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("swept %d stale raw capture(s)", removed)


def _make_sinks(station_id: int) -> list:
    sinks = [FileSink(config.STREAM_DIR, station_id,
                      raw_capture=config.STREAM_RAW_CAPTURE,
                      station_name=_station_names.get(station_id, ""))]
    # Additive, like the rover sinks below: the archive sink is never replaced.
    # A station missing from STREAM_CASTER_PASSWORDS gets no caster push at all -
    # unless auto-provisioning has just given it one, which happens in
    # _provision_caster_mountpoint() before this function is ever called.
    password = _bundled_caster_password(station_id)
    if password is not None:
        sinks.append(NtripCasterSink(
            config.STREAM_CASTER_HOST, config.STREAM_CASTER_PORT,
            str(station_id), password,
        ))
        # A base's own ARP is what Millipede's NEAR lookup needs in the
        # sourcetable. Attached to every base with a mountpoint, not only an
        # auto-provisioned one: it is also what spares a hand-provisioned
        # station the "re-run generate_config.py once it has archived a 1005"
        # step, since the position now arrives live.
        if _caster_provision is not None and _station_roles.get(station_id) == ROLE_BASE:
            sinks.append(CasterArpSink(station_id, _caster_provision))
    # Any further casters named in STREAM_CASTER_TARGETS. Same station, same
    # RTCM3, one sink per target - a station can go to the bundled caster above
    # and any number of these at once. Each is independent: one caster being
    # down or unprovisioned costs nothing but its own sink's reconnects.
    for target in config.STREAM_CASTER_TARGETS:
        password = target.passwords.get(station_id)
        if password:
            sinks.append(NtripCasterSink(target.host, target.port, str(station_id), password))
        else:
            logger.warning(
                "station %s: caster target %r has no password for it "
                "(STREAM_CASTER_%s_PASSWORDS) - not forwarded there",
                station_id, target.name, target.name.upper().replace("-", "_"),
            )
    # A base (statically configured in STREAM_ROVER_BASES, or auto-registered
    # via _ensure_base_router() the moment it identifies with role=base) feeds
    # its own rovers in-process: every RTCM3 frame it sends is published
    # straight into that base's router. Additive - the base keeps its archive.
    # BaseArpSink (a RoverSourceSink superset) when auto-discovery is on, so
    # this base's ARP becomes a candidate for ANY role=rover station's
    # nearest-base pick, not just its own static rover list.
    if station_id in _base_routers:
        if _discovery is not None:
            sinks.append(BaseArpSink(_base_routers[station_id], station_id, _discovery))
        else:
            sinks.append(RoverSourceSink(_base_routers[station_id]))
    # A station that identified as role=rover (frames.ROLE_ROVER) gets its own
    # UBX-NAV-PVT fed to RoverAutoDiscovery, so it can be subscribed to its
    # nearest base with no config at all. _station_roles is populated in
    # handle_connection() from this station's very first IDENT, before this
    # function is ever called for it (sinks are built once, on first
    # get_or_create()) - see the ordering note there.
    if _discovery is not None and _station_roles.get(station_id) == ROLE_ROVER:
        sinks.append(RoverPositionSink(station_id, _discovery))
    return sinks


registry = StationRegistry(_make_sinks)

# Created in run() when rovers are configured; None means the external-NTRIP
# downlink is off.
_rover_router: RoverRouter | None = None

# station id -> role declared in its most recent IDENT. Populated in
# handle_connection() BEFORE registry.get_or_create() (which only calls
# _make_sinks() on a station's first-ever identification this process), so
# _make_sinks() above can see the role the very first time it runs.
_station_roles: dict[int, int] = {}

# station id -> station_name from its most recent IDENT, raw as the device sent
# it (""  for firmware that predates the field). Populated alongside
# _station_roles and for the same reason: _make_sinks() runs exactly once per
# station per process and must already know the name to label the archive.
#
# A station that renames itself mid-process keeps the label its sinks were built
# with until the server restarts. That is deliberate - swapping the output
# directory under a live connection would split one session across two paths.
_station_names: dict[int, str] = {}

# None unless STREAM_ROVER_AUTO_ENABLE - see rover_discovery.py.
_discovery: RoverAutoDiscovery | None = None

# None unless STREAM_CASTER_AUTO_ENABLE - see caster_provision.py. Owns the
# bundled caster's station set for the life of the process.
_caster_provision: CasterAutoProvision | None = None

# The live, hot-reloadable copy of STREAM_ROVER_BASES (base -> manually pinned
# rovers). Starts as config.STREAM_ROVER_BASES; POST /stream/rover/reload or
# SIGHUP replace it via _apply_rover_bases(), which diffs old vs new and
# leaves anything RoverAutoDiscovery is managing untouched.
_manual_bases: dict[int, set[int]] = {}

# In-fleet routing: base station id -> its RoverRouter. Populated in run() before
# the server starts listening, so _make_sinks() can attach a RoverSourceSink to a
# base the moment it connects. Empty means no in-fleet routing is configured.
_base_routers: dict[int, "RoverRouter"] = {}
# Strong references to the auto-registered routers' run() tasks - see
# _auto_register_base(). Without them those tasks are garbage-collectable.
_router_tasks: dict[int, asyncio.Future] = {}


def _ensure_base_router(station_id: int) -> None:
    """Create a RoverRouter for `station_id` the moment it identifies as a base.

    Only for station ids explicitly trusted as a correction source
    (STREAM_ROVER_AUTO_BASE_STATIONS) - a bare role=base claim on the wire is
    not enough, see rover_discovery.py's module docstring. A station already
    statically configured in STREAM_ROVER_BASES already has a router from
    run() and this is a no-op for it.
    """
    if station_id in _base_routers:
        return
    if station_id not in config.STREAM_ROVER_AUTO_BASE_STATIONS:
        return
    router = RoverRouter(registry, set(), config.STREAM_ROVER_QUEUE,
                         config.STREAM_ROVER_RTCM_TYPES, base_station_id=station_id)
    _base_routers[station_id] = router
    # ⚠ KEEP THE TASK. The event loop holds only a weak reference to a task, and
    # router.run() parks on an asyncio.Event nobody else references - so a bare
    # ensure_future() left the whole cycle unreachable. The garbage collector was
    # then free to destroy it, and destroying it runs run()'s `finally`, which
    # CANCELS EVERY ROVER SENDER on this base: auto-registered bases silently
    # stopped handing out corrections, whenever a collection happened to run.
    # Found 2026-09-17 when one more test file shifted GC timing enough for
    # test_rover_auto_e2e to see "Task was destroyed but it is pending!".
    # pipeline.py's file-transfer task documents the same trap.
    _router_tasks[station_id] = asyncio.ensure_future(router.run())
    logger.info("rover downlink: auto-registered base %d (role=base, trusted)", station_id)


def _bundled_caster_password(station_id: int) -> str | None:
    """This station's password on the bundled caster, or None for no push.

    STREAM_CASTER_ENABLE gates the hand-configured path. The auto-provisioner
    being on is its own enable for what it provisions: it does write
    STREAM_CASTER_ENABLE=true into .env, but that would only take effect on the
    next start, and not having to wait for one is the entire point.
    """
    if config.STREAM_CASTER_ENABLE and station_id in config.STREAM_CASTER_PASSWORDS:
        return config.STREAM_CASTER_PASSWORDS[station_id]
    if _caster_provision is not None:
        return _caster_provision.password_for(station_id)
    return None


def _ensure_caster_sink(session, password: str) -> None:
    """Attach the bundled caster's sinks to a session whose sinks already exist.

    _make_sinks() runs exactly once per station per process, so a station that
    becomes a base later in the life of that process - reconfigured on the
    device, or simply provisioned during this very connect - would otherwise
    have to wait for a restart. A restart is the manual step this feature
    exists to remove; it must not reappear here.
    """
    host, port, mount = config.STREAM_CASTER_HOST, config.STREAM_CASTER_PORT, str(session.station_id)
    # Compared on the full triple, not on the type: STREAM_CASTER_TARGETS put
    # NtripCasterSinks for other casters on this same session.
    if any(isinstance(s, NtripCasterSink) and (s.host, s.port, s.mountpoint) == (host, port, mount)
           for s in session.sinks):
        return
    session.sinks.append(NtripCasterSink(host, port, mount, password))
    if _caster_provision is not None and not any(isinstance(s, CasterArpSink) for s in session.sinks):
        session.sinks.append(CasterArpSink(session.station_id, _caster_provision))
    logger.info("caster: mountpoint %s attached to the live session", mount)


def _provision_caster_mountpoint(station_id: int) -> None:
    """Give a station that just identified as role=base a caster mountpoint.

    Called from handle_connection() before get_or_create(), for the same reason
    _ensure_base_router() is: a station connecting for the first time this
    process then already has its password when _make_sinks() runs. One that
    already has a session gets the sink appended instead.
    """
    if _caster_provision is None:
        return
    try:
        password = _caster_provision.on_base_ident(station_id)
    except Exception:  # noqa: BLE001 - provisioning must never kill a connection
        logger.exception("caster: provisioning station %d failed", station_id)
        return
    if password is None:
        return
    session = registry.get(station_id)
    if session is not None:
        _ensure_caster_sink(session, password)


# ==========================================================================
# TCP data plane
# ==========================================================================
async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer_info = writer.get_extra_info("peername")
    peer = f"{peer_info[0]}:{peer_info[1]}" if peer_info else "?"
    logger.info("connection from %s", peer)

    framer = StreamFramer()
    session = None
    pending_raw = bytearray()  # bytes received before IDENT told us who this is
    prev_resync = prev_garbage = 0

    try:
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), config.STREAM_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("%s idle for %ds - closing", peer, config.STREAM_IDLE_TIMEOUT)
                break
            if not data:
                break

            chunk_accounted = False
            if session is None:
                pending_raw.extend(data)
                if len(pending_raw) > config.STREAM_PRE_IDENT_CAP:
                    logger.warning("%s sent %d bytes without IDENT - dropping", peer, len(pending_raw))
                    break

            for frame in framer.feed(data):
                if session is None:
                    if not is_ident(frame):
                        continue  # cannot attribute anything before IDENT
                    ident = decode_ident(frame)
                    if ident is None:
                        continue
                    station_id = ident.station_id

                    # Both must happen before get_or_create(): _make_sinks()
                    # (called on this station's first-ever identification this
                    # process) reads _station_roles and _base_routers to decide
                    # what to attach, and only gets one chance to do so.
                    _station_roles[station_id] = ident.role
                    _station_names[station_id] = ident.name
                    if ident.role == ROLE_BASE:
                        _ensure_base_router(station_id)
                        _provision_caster_mountpoint(station_id)

                    session = registry.get_or_create(station_id)
                    session.bind(writer, peer)
                    logger.info("station %s identified from %s (role=%d, name=%r)",
                                station_id, peer, ident.role, ident.name)
                    if _discovery is not None:
                        _discovery.on_ident(station_id, ident.role)

                    # Flush everything received so far (including this chunk).
                    session.bytes_rx += len(pending_raw)
                    for sink in session.sinks:
                        sink.on_raw(bytes(pending_raw))
                    pending_raw.clear()
                    chunk_accounted = True
                    continue

                route_frame(session, frame)

            if session is not None:
                if not chunk_accounted:
                    session.bytes_rx += len(data)
                    for sink in session.sinks:
                        sink.on_raw(data)
                session.resync_events += framer.resync_events - prev_resync
                session.garbage_bytes += framer.garbage_bytes - prev_garbage
            prev_resync, prev_garbage = framer.resync_events, framer.garbage_bytes

    except (ConnectionResetError, BrokenPipeError) as exc:
        logger.info("%s connection reset: %s", peer, exc)
    except Exception:  # noqa: BLE001 - one bad connection must not kill the server
        logger.exception("%s handler error", peer)
    finally:
        if session is not None:
            # unbind() is a no-op if a newer connection already superseded
            # this one (writer-scoped, see StationSession.unbind) - only tell
            # discovery about a disconnect that actually took the station
            # offline, not a stale handler winding down after being replaced.
            was_live = session.writer is writer
            session.unbind(writer)
            logger.info("station %s disconnected (%s)", session.station_id, peer)
            if was_live and _discovery is not None:
                _discovery.on_disconnect(session.station_id)
        else:
            logger.info("%s disconnected without IDENT", peer)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


# ==========================================================================
# HTTP admin / CLI control plane
# ==========================================================================
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    if api_key != config.API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return api_key


class GnssTunnelRequest(BaseModel):
    """Open a GNSS maintenance tunnel (K-01 Etappe 2).

    The baud is the device's UART1 rate, NOT anything about this TCP hop - over
    TCP there is no baud rate to match, which is exactly the trap the first bench
    attempt fell into. The rate that matters is the
    one the receiver is speaking, and it is pinned here rather than negotiated.
    """

    # "auto" is the normal case: the LIVE link rate, which on a configured device
    # is 921600 and is NOT the rate the boot-time baud scan reported.
    baud: str = Field("auto", pattern=r"^(auto|\d{4,6})$")
    # Receiver stuck in its boot ROM after a failed update: power-cycle it and
    # open a SILENT tunnel, so the tool's training sequence is the first thing
    # the fresh ROM hears and binds its auto-baud to.
    rescue: bool = False
    # Rescue only: the rate the tool moves to for the download.
    # ⚠ Over UART3 the device detects the switch itself, by the ~1.2 s pause the
    # tool makes. Over THIS path it does not - see the switch endpoint below.
    # Passing it here still matters: it becomes the default for a later
    # `gnsstunnel/switch` with no rate, so the operator names it once, while
    # planning, instead of during a running flash.
    switch_baud: int | None = Field(None, ge=4800, le=921600)
    # Device-side session timeout, counted only against operator silence.
    idle_s: int | None = Field(None, ge=10, le=1800)
    # 0 = let the OS choose. Always bound to loopback; see gnss_tunnel.py.
    port: int = Field(0, ge=0, le=65535)


class GnssTunnelSwitchRequest(BaseModel):
    """Move a running session to the download rate.

    `baud` omitted means the rate the session was opened with (--switch-baud):
    the operator is typing this while watching a flash tool count, and making
    them repeat a number at that moment is how typos happen.
    """

    baud: int | None = Field(None, ge=4800, le=921600)


class CliRequest(BaseModel):
    # Printable ASCII only — encode_cmd_request() puts the command on the wire as
    # ascii. Rejecting here answers 422 instead of letting the encode fail deeper in
    # and surface as a 500 for what is a caller error.
    cmd: str = Field(..., min_length=1, max_length=200, pattern=r"^[\x20-\x7E]+$")
    timeout: float | None = Field(None, gt=0, le=3600)


admin = FastAPI(title="Streaming Server", version="0.1.0")

# The configuration generator. Mounted here rather than on the batch server
# because this app binds to loopback: the generator prefills a device file with
# this installation's own API key and remote-CLI token, so it must not be
# reachable from outside. Its routes carry a second, independent loopback check
# of their own - see configgen/router.py.
try:
    from configgen.router import build_router as _build_configgen_router

    admin.include_router(_build_configgen_router(config), prefix="/config",
                         tags=["Config"])
except Exception:  # noqa: BLE001 - the generator is optional, the server is not
    logger.exception("config generator not mounted")


@admin.get("/health", tags=["System"])
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stations": len(registry.all()),
        "connected": sum(1 for s in registry.all() if s.connected),
    }


@admin.get("/stream/stations", tags=["Stream"])
async def list_stations(_: str = Depends(verify_api_key)):
    return {"stations": [
        {**s.status(), "role": _station_roles.get(s.station_id, 0)}
        for s in registry.all()
    ]}


@admin.get("/stream/rover", tags=["Stream"])
async def rover_status(_: str = Depends(verify_api_key)):
    """Correction downlink counters.

    The pair worth watching is frames_in against frames_out: equal means every
    correction reached a rover, a growing gap means they are being dropped, and
    dropped_full vs dropped_offline says which - the queue overflowing (the
    device or the link cannot keep up) or the station not being there.

    One entry per router under "routers": "base_<id>" for an in-fleet base source,
    "ntrip" for the external caster source. "auto_discovery" (present whenever
    STREAM_ROVER_AUTO_ENABLE) lists every role=rover candidate seen so far and,
    for each, which base it is subscribed to and the baseline distance that
    chose it - so a wrong subscription shows up here instead of being inferred
    from a bad fix. A candidate with subscribed_base=null and has_fix=false is
    holding for its first 3D fix (see rover_discovery.py's module docstring).

    subscribed_base=null does NOT by itself mean "waiting": an entry carrying
    "not_a_candidate" is a station that will never be subscribed and is listed
    only so its absence from the subscription set reads as a decision. Today
    that is role=rover_ntrip, which fetches its corrections from an NTRIP caster
    itself and discards anything pushed to it.
    """
    routers = {f"base_{base_id}": r.status() for base_id, r in _base_routers.items()}
    if _rover_router is not None:
        routers["ntrip"] = _rover_router.status()
    if not routers and _discovery is None:
        return {"enabled": False}
    result = {"enabled": True, "routers": routers}
    if _discovery is not None:
        result["auto_discovery"] = _discovery.status()
    return result


@admin.get("/stream/caster", tags=["Stream"])
async def caster_status(_: str = Depends(verify_api_key)):
    """Mountpoints on the bundled caster, and the extra targets pushed to.

    "auto_provisioned" separates a mountpoint this server created off a role=base
    IDENT from one that was configured by hand or by caster/setup.sh - the two
    are otherwise indistinguishable, and only the first will reappear on its own
    if the files are ever regenerated. "position" is null until that station's
    own RTCM 1005/1006 ARP has been decoded; Millipede's NEAR entry needs at
    least one non-null to mean anything.

    "caster_pid" being null means nothing is running this config: pushes will
    fail their handshake, and a provisioning wrote correct files that nobody
    reloaded.
    """
    result = {
        "bundled": _caster_provision.status() if _caster_provision is not None
        else {"enabled": False, "mountpoints": {
            str(s): {"auto_provisioned": False, "position": None}
            for s in sorted(config.STREAM_CASTER_PASSWORDS)}},
        "targets": [
            {"name": t.name, "host": t.host, "port": t.port,
             "stations": sorted(t.passwords)}
            for t in config.STREAM_CASTER_TARGETS
        ],
    }
    return result


class RoverSubscribeRequest(BaseModel):
    base: int
    rover: int


@admin.post("/stream/rover/subscribe", tags=["Stream"])
async def rover_subscribe(req: RoverSubscribeRequest, _: str = Depends(verify_api_key)):
    """Manually subscribe a rover to a base's router, live - no restart.

    The base must already have a router (statically configured, or already
    auto-registered via a role=base IDENT) - this endpoint only ever adds a
    rover to an existing router, it never creates one, which is the same
    trust boundary _ensure_base_router() enforces for role=base.
    """
    router = _base_routers.get(req.base)
    if router is None:
        raise HTTPException(404, f"base {req.base} has no router (not configured and "
                                  f"not yet identified as a trusted base)")
    added = router.add_rover(req.rover)
    return {"ok": True, "base": req.base, "rover": req.rover, "added": added}


@admin.delete("/stream/rover/subscribe", tags=["Stream"])
async def rover_unsubscribe(req: RoverSubscribeRequest, _: str = Depends(verify_api_key)):
    router = _base_routers.get(req.base)
    if router is None:
        raise HTTPException(404, f"base {req.base} has no router")
    removed = router.remove_rover(req.rover)
    return {"ok": True, "base": req.base, "rover": req.rover, "removed": removed}


def _reload_rover_bases_from_env() -> dict[int, set[int]]:
    """Re-read STREAM_ROVER_BASES straight from .env - never os.environ/config,
    which are process-lifetime; this is the whole point of a hot reload."""
    values = dotenv_values(config.BASE_DIR / ".env")
    return config.parse_rover_bases(values.get("STREAM_ROVER_BASES", "") or "")


def _apply_rover_bases(new_map: dict[int, set[int]]) -> None:
    """Diff `new_map` against the live `_manual_bases` and add/remove only the
    difference - existing connections and anything RoverAutoDiscovery
    subscribed on top are untouched. Never creates or destroys a router: a
    base with no router yet (not in STREAM_ROVER_BASES at process start, and
    not auto-registered) is skipped with a warning, same as it always was.
    """
    global _manual_bases
    all_bases = set(_manual_bases) | set(new_map)
    for base_id in all_bases:
        old_rovers = _manual_bases.get(base_id, set())
        new_rovers = new_map.get(base_id, set())
        router = _base_routers.get(base_id)
        if router is None:
            if new_rovers:
                logger.warning("rover reload: base %d has no router - %s not applied",
                               base_id, sorted(new_rovers))
            continue
        for rover_id in sorted(new_rovers - old_rovers):
            router.add_rover(rover_id)
            logger.info("rover reload: base %d + rover %d", base_id, rover_id)
        for rover_id in sorted(old_rovers - new_rovers):
            router.remove_rover(rover_id)
            logger.info("rover reload: base %d - rover %d", base_id, rover_id)
    _manual_bases = new_map
    if _discovery is not None:
        _discovery._manual_rovers = _compute_manual_rovers(new_map)


def _compute_manual_rovers(bases_map: dict[int, set[int]]) -> set[int]:
    manual = set(config.STREAM_ROVER_STATIONS)
    for rovers in bases_map.values():
        manual |= rovers
    return manual


@admin.post("/stream/rover/reload", tags=["Stream"])
async def rover_reload(_: str = Depends(verify_api_key)):
    """Re-read STREAM_ROVER_BASES from .env and apply the diff live. The
    SIGHUP handler in run() calls the same _apply_rover_bases()."""
    new_map = _reload_rover_bases_from_env()
    _apply_rover_bases(new_map)
    return {"ok": True, "bases": {str(k): sorted(v) for k, v in new_map.items()}}


@admin.post("/stream/{station_id}/cli", tags=["Stream"])
async def send_cli(station_id: int, req: CliRequest, _: str = Depends(verify_api_key)):
    session = registry.get(station_id)
    if session is None:
        raise HTTPException(404, f"unknown station {station_id}")
    if not session.connected:
        raise HTTPException(409, f"station {station_id} not connected")

    timeout = req.timeout
    if timeout is None:
        # download/downloadfw close the socket, transfer, then reconnect to answer.
        timeout = (
            config.STREAM_CLI_TRANSFER_TIMEOUT
            if is_download_class(req.cmd)
            else config.STREAM_CLI_TIMEOUT
        )

    try:
        text = await session.send_cli(req.cmd, config.STREAM_CLI_SECRET, timeout)
    except ConnectionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except asyncio.TimeoutError:
        raise HTTPException(504, f"no response within {timeout}s") from None

    return {"ok": True, "station_id": station_id, "cmd": req.cmd, "response": text}


# ==========================================================================
# GNSS maintenance tunnel (K-01 Etappe 2)
# ==========================================================================
@admin.post("/stream/{station_id}/gnsstunnel/open", tags=["Stream"])
async def gnsstunnel_open(station_id: int, req: GnssTunnelRequest,
                          _: str = Depends(verify_api_key)):
    """Start a bridge session on the device and expose it as a local TCP port.

    Order matters: the listener is bound BEFORE the device is told to open its
    side. The reverse order leaves a window in which the device is tunnelling
    into a port that does not exist yet - and because the device suspends its
    receiver handling for the whole session, that window costs a real session and
    a receiver recovery, not just a retry.
    """
    session = registry.get(station_id)
    if session is None:
        raise HTTPException(404, f"unknown station {station_id}")
    if not session.connected:
        raise HTTPException(409, f"station {station_id} not connected")
    if tunnel_registry.get(station_id) is not None:
        raise HTTPException(409, f"station {station_id} already has a tunnel open")

    tunnel = await tunnel_registry.open(session, config.STREAM_ADMIN_HOST, req.port)

    # Build the device-side command. Same syntax the bench CLI uses; the device
    # picks the SOCKET transport by itself because the command arrives over the
    # streaming socket (see gnssbridge_transport() in cli.c).
    if req.rescue:
        baud = req.baud if req.baud != "auto" else "9600"
        parts = ["gnssbridge", "rescue", baud]
        parts.append(str(req.switch_baud or 0))
        if req.idle_s:
            parts.append(str(req.idle_s))
    else:
        parts = ["gnssbridge", req.baud]
        if req.idle_s:
            parts.append(str(req.idle_s))
    cmd = " ".join(parts)

    try:
        text = await session.send_cli(cmd, config.STREAM_CLI_SECRET,
                                      config.STREAM_CLI_TIMEOUT)
    except Exception as exc:
        # The device refused or did not answer: tear the listener down again
        # rather than leave a port that pipes into nothing.
        await tunnel_registry.close(station_id)
        raise HTTPException(502, f"device did not start the bridge: {exc}") from exc

    # A device that ANSWERED can still have refused. The CLI transport only says
    # the command arrived; whether a session started is in the text. Without this
    # check the listener stayed open and the tool printed "tunnel open" for a
    # device that had said "bridge NOT started" - found 2026-09-17 when the
    # firmware began refusing sessions during a receiver recovery. A port that
    # pipes into a refused session looks exactly like a broken link.
    if "NOT started" in text:
        await tunnel_registry.close(station_id)
        raise HTTPException(409, f"device refused the session: {text.strip()}")

    return {
        "ok": True,
        "station_id": station_id,
        "cmd": cmd,
        "device_response": text,
        "tunnel": tunnel.status(),
        "hint": (
            f"ssh -L {tunnel.port}:127.0.0.1:{tunnel.port} <server>, then point the "
            "virtual COM port at localhost:%d with NVT/RFC2217 OFF" % tunnel.port
        ),
    }


@admin.post("/stream/{station_id}/gnsstunnel/switch", tags=["Stream"])
async def gnsstunnel_switch(station_id: int, req: GnssTunnelSwitchRequest,
                            _: str = Depends(verify_api_key)):
    """Move a RUNNING session to the download rate, on the operator's word.

    The device can infer this from a pause in the operator's traffic, and over a
    cable it does. Over LTE it cannot: the modem delivers the downlink in bursts
    that swallow the pause, and ubxfwupdate retries every 1.0 s against the
    device's 900 ms threshold, so every retry restarts the clock. Measured
    2026-09-21: the tool switched at t=4.7 s, gave up at 7.9 s, and the bridge
    followed at ~8.8 s - after the run had already failed.

    The operator, meanwhile, is reading "Setting baudrate to N" on their own
    screen. This endpoint turns that knowledge into a statement.

    No tunnel object is touched: the rate lives on the DEVICE side of the link,
    and the command rides the same token-protected CLI as open and close. We do
    require an open tunnel, because a switch without one is certainly a mistake.
    """
    if tunnel_registry.get(station_id) is None:
        raise HTTPException(status_code=409,
                            detail=f"no GNSS tunnel is open for station {station_id}")

    session = registry.get(station_id)
    if session is None or not session.connected:
        raise HTTPException(status_code=409,
                            detail=f"station {station_id} not connected")

    cmd = "gnssbridge switch" + (f" {req.baud}" if req.baud else "")
    try:
        device_response = await session.send_cli(
            cmd, config.STREAM_CLI_SECRET, config.STREAM_CLI_TIMEOUT)
    except Exception as exc:
        raise HTTPException(status_code=504,
                            detail=f"station {station_id} did not answer: {exc}")

    # ⚠ The device's ANSWER decides, not the fact that the command arrived. The
    # same distinction cost a bench run on 2026-09-17, when `open` reported a
    # tunnel for a session the device had refused.
    text = (device_response or "").strip()
    if "REFUSED" in text or "no bridge session" in text:
        raise HTTPException(status_code=409, detail=f"device refused: {text}")

    return {"ok": True, "station_id": station_id, "device_response": text}


@admin.post("/stream/{station_id}/gnsstunnel/close", tags=["Stream"])
async def gnsstunnel_close(station_id: int, _: str = Depends(verify_api_key)):
    """Close the local listener and ask the device to end its session."""
    tunnel = tunnel_registry.get(station_id)
    status_before = tunnel.status() if tunnel is not None else None

    # Tell the device first here - the opposite of open(), and for the same
    # reason: whichever side is torn down second must not be the one still
    # sending. A device left in a session is the expensive half (it holds the
    # receiver), so it is stopped first and the listener follows.
    device_response = None
    session = registry.get(station_id)
    if session is not None and session.connected:
        try:
            device_response = await session.send_cli(
                "gnssbridge stop", config.STREAM_CLI_SECRET, config.STREAM_CLI_TIMEOUT)
        except Exception as exc:
            # Still tear our side down: the device has an idle timeout and a hard
            # session cap of its own precisely so a lost operator cannot strand it.
            device_response = f"(no answer: {exc})"

    closed = await tunnel_registry.close(station_id)
    return {
        "ok": True,
        "station_id": station_id,
        "listener_closed": closed,
        "device_response": device_response,
        "stats": status_before,
    }


@admin.get("/stream/gnsstunnel", tags=["Stream"])
async def gnsstunnel_status(_: str = Depends(verify_api_key)):
    return {"tunnels": tunnel_registry.all_status()}


# ==========================================================================
# Lifecycle
# ==========================================================================
async def run() -> None:
    global _rover_router, _discovery, _manual_bases, _caster_provision

    setup_logging()
    config.STREAM_DIR.mkdir(parents=True, exist_ok=True)
    sweep_stale_raw()

    # --- NTRIP caster push (optional) ---------------------------------------
    if config.STREAM_CASTER_ENABLE:
        if config.STREAM_CASTER_PASSWORDS:
            logger.info("ntrip caster push: %s:%d, stations %s",
                        config.STREAM_CASTER_HOST, config.STREAM_CASTER_PORT,
                        sorted(config.STREAM_CASTER_PASSWORDS))
        else:
            logger.warning(
                "STREAM_CASTER_ENABLE is set but STREAM_CASTER_PASSWORDS is empty "
                "- no station has a mountpoint, nothing will be pushed")
    for target in config.STREAM_CASTER_TARGETS:
        logger.info("ntrip caster push: %s:%d (%s), stations %s",
                    target.host, target.port, target.name, sorted(target.passwords))

    # --- Automatic mountpoints on the bundled caster (optional) -------------
    # Only the bundled caster: STREAM_CASTER_TARGETS are casters whose config
    # this server does not own, so a station is provisioned there by hand as
    # before. See caster_provision.py.
    if config.STREAM_CASTER_AUTO_ENABLE:
        _caster_provision = CasterAutoProvision(
            config.BASE_DIR / ".env",
            config.STREAM_CASTER_STATIONS,
            config.STREAM_CASTER_PASSWORDS,
            etc_dir=config.STREAM_CASTER_ETC_DIR,
        )
        logger.info("caster auto-provisioning: on, %s, mountpoints %s",
                    config.STREAM_CASTER_ETC_DIR, sorted(_caster_provision.stations))
        if not (config.STREAM_CASTER_ETC_DIR / "caster.yaml").exists():
            logger.warning(
                "caster auto-provisioning is on but %s does not exist - run "
                "caster/setup.sh, or point STREAM_CASTER_ETC_DIR at the caster "
                "that should actually receive these pushes",
                config.STREAM_CASTER_ETC_DIR / "caster.yaml")

    # --- RTK rover correction downlink (optional) --------------------------
    # Off unless stations are named AND a source is configured. A half-configured
    # downlink must not start: a router with no source would sit there reporting
    # zero frames, which reads like a broken caster rather than like a setting
    # nobody filled in.
    extra_tasks = []
    if config.STREAM_ROVER_STATIONS:
        _rover_router = RoverRouter(registry, config.STREAM_ROVER_STATIONS,
                                    config.STREAM_ROVER_QUEUE,
                                    config.STREAM_ROVER_RTCM_TYPES)
        extra_tasks.append(_rover_router.run())
        if config.STREAM_ROVER_RTCM_TYPES:
            logger.info("rover downlink: forwarding only RTCM3 types %s",
                        sorted(config.STREAM_ROVER_RTCM_TYPES))
        if config.STREAM_NTRIP_MOUNT and config.STREAM_NTRIP_USER:
            extra_tasks.append(ntrip_source(
                _rover_router,
                config.STREAM_NTRIP_HOST, config.STREAM_NTRIP_PORT,
                config.STREAM_NTRIP_MOUNT, config.STREAM_NTRIP_USER,
                config.STREAM_NTRIP_PASS,
            ))
            logger.info("rover downlink: %s -> stations %s",
                        config.STREAM_NTRIP_MOUNT, sorted(config.STREAM_ROVER_STATIONS))
        else:
            logger.warning(
                "STREAM_ROVER_STATIONS is set but no NTRIP source is configured "
                "(STREAM_NTRIP_MOUNT / STREAM_NTRIP_USER) - no corrections will flow")

    # --- In-fleet base -> rover routing (optional) -------------------------
    # One router per base. The source is a RoverSourceSink (or BaseArpSink, if
    # auto-discovery is on) attached to the base's stream in _make_sinks(), so
    # _base_routers must be populated before the server starts listening -
    # it is, right here. No external caster involved.
    _manual_bases = dict(config.STREAM_ROVER_BASES)
    for base_id, rover_ids in config.STREAM_ROVER_BASES.items():
        router = RoverRouter(registry, rover_ids, config.STREAM_ROVER_QUEUE,
                             config.STREAM_ROVER_RTCM_TYPES, base_station_id=base_id)
        _base_routers[base_id] = router
        extra_tasks.append(router.run())
        logger.info("rover downlink: in-fleet base %d -> stations %s",
                    base_id, sorted(rover_ids))

    # --- Automatic rover -> nearest-base subscription (optional) -----------
    # Needs no source
    # of its own: it only ever calls add_rover()/remove_rover() on routers
    # that already exist (the ones just built above, plus any a role=base
    # IDENT registers later via _ensure_base_router()). A station already
    # hand-pinned above (or in STREAM_ROVER_STATIONS) is never touched by it.
    if config.STREAM_ROVER_AUTO_ENABLE:
        _discovery = RoverAutoDiscovery(
            _base_routers, _compute_manual_rovers(_manual_bases),
            config.STREAM_ROVER_MAX_BASELINE_KM, config.STREAM_ROVER_SWITCH_MARGIN_KM,
        )
        logger.info(
            "rover auto-discovery: on (max baseline %.0f km, switch margin %.0f km, "
            "trusted auto-base stations %s)",
            config.STREAM_ROVER_MAX_BASELINE_KM, config.STREAM_ROVER_SWITCH_MARGIN_KM,
            sorted(config.STREAM_ROVER_AUTO_BASE_STATIONS) or "(none - only statically "
            "configured bases can be auto-subscribed to)",
        )

    tcp = await asyncio.start_server(handle_connection, config.STREAM_HOST, config.STREAM_PORT)
    logger.info("TCP data plane on %s:%d", config.STREAM_HOST, config.STREAM_PORT)
    if not config.STREAM_CLI_SECRET:
        logger.warning("STREAM_CLI_SECRET is empty - remote CLI is unauthenticated")
    else:
        # A token longer than the device's field can never match.
        #
        # The DEVICE is the side that authenticates: the server puts
        # [tok_len][token] in front of every command, and the device compares
        # the length first, then the bytes. Its field is fixed-size, so a longer
        # token is truncated when it reads its configuration - silently, because
        # truncation is not an error there. The two values then still look
        # identical wherever a human compares them: the .env and the device's own
        # file both hold the full string. Only the comparison inside the device
        # sees a shorter one, and every command comes back "auth failed".
        #
        # That is expensive to debug, because "auth failed" points at the VALUE
        # while the fault is in the LENGTH. So it is said here, at startup, where
        # the mismatch actually originates.
        try:
            from configgen.schema import load as _load_device_schema

            limit = _load_device_schema().by_key["streaming_cli_secret"]["max_len"]
            usable = limit - 1                      # the field keeps a terminator
            if len(config.STREAM_CLI_SECRET) > usable:
                logger.warning(
                    "STREAM_CLI_SECRET is %d characters, but a device stores at "
                    "most %d - it truncates the rest without complaining and then "
                    "rejects every remote CLI command with 'auth failed'. Shorten "
                    "it on both sides, or leave it empty.",
                    len(config.STREAM_CLI_SECRET), usable)
        except Exception:  # noqa: BLE001 - the schema is optional, the server is not
            logger.debug("could not check STREAM_CLI_SECRET against the device schema",
                         exc_info=True)

    uv = uvicorn.Server(
        uvicorn.Config(
            admin,
            host=config.STREAM_ADMIN_HOST,
            port=config.STREAM_ADMIN_PORT,
            log_level="warning",
            access_log=False,
        )
    )
    logger.info("admin API on %s:%d", config.STREAM_ADMIN_HOST, config.STREAM_ADMIN_PORT)

    def _on_sighup() -> None:
        logger.info("SIGHUP received - reloading STREAM_ROVER_BASES from .env")
        try:
            _apply_rover_bases(_reload_rover_bases_from_env())
        except Exception:  # noqa: BLE001 - a bad reload must not kill the server
            logger.exception("rover reload via SIGHUP failed")

    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, _on_sighup)
    except (NotImplementedError, AttributeError):
        logger.debug("SIGHUP reload unavailable on this platform - use POST /stream/rover/reload")

    try:
        async with tcp:
            await asyncio.gather(tcp.serve_forever(), uv.serve(), *extra_tasks)
    finally:
        registry.close_all()
