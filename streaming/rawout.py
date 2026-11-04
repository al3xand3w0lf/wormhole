"""Raw receiver stream out — every station's receiver output, live, over NTRIP.

The other direction from everything else in this package: not into the server,
not to a caster we push to, but *out* to whoever asks, as an NTRIP caster
would serve it. One listener, one mountpoint per station (mountpoint == station
id), Basic auth per account, TLS optional. See "Raw stream out" in README.md.

What is forwarded is exactly what route_frame() hands to on_ubx / on_rtcm3, in
the order it does so: the receiver's own output, whole frames only. Private
0xF0 frames never reach those two callbacks, so they never reach a client.

The one hard rule here is the one the whole server is built on: a client must
never slow down reception. publish() therefore only calls writer.write() -
non-blocking, no await, no queue - and a client whose send buffer grows past
the limit is cut off (transport.abort()), not thinned out. A real-time consumer
is better served by a clean cut it notices than by a stream with silent holes.
"""

import asyncio
import base64
import binascii
import hmac
import logging
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .sinks import Sink

logger = logging.getLogger("streaming")

SERVER_AGENT = "wormhole-rawout/1.0"

_HEADER_TIMEOUT = 10.0
_HEADER_LIMIT = 8192
_AUTH_LOG_INTERVAL = 60.0


# ==========================================================================
# Accounts
# ==========================================================================
@dataclass(frozen=True)
class Account:
    user: str
    password: str
    stations: frozenset[int] | None  # None = every station

    def may_read(self, station_id: int) -> bool:
        return self.stations is None or station_id in self.stations


def parse_users(text: str) -> tuple[dict[str, Account], list[str]]:
    """`user:password:stations,...` -> accounts, plus one error per bad entry.

    `stations` is `*` or `1001+1020`. The password may contain ':' (user is
    everything before the first one, stations everything after the last one),
    but not ',' - that separates entries. Errors are returned, not raised:
    config is parsed at import time, and the decision to refuse to start
    belongs to run(), which can say so properly.
    """
    accounts: dict[str, Account] = {}
    errors: list[str] = []
    for entry in (e.strip() for e in text.split(",")):
        if not entry:
            continue
        head, sep, stations = entry.rpartition(":")
        user, sep2, password = head.partition(":")
        if not sep or not sep2 or not user or not password or not stations:
            errors.append(f"{entry.split(':', 1)[0]!r}: expected user:password:stations")
            continue
        if stations.strip() == "*":
            allowed = None
        else:
            ids = [s.strip() for s in stations.split("+")]
            if not all(s.isdigit() for s in ids):
                errors.append(f"{user!r}: stations must be '*' or ids joined by '+', got {stations!r}")
                continue
            allowed = frozenset(int(s) for s in ids)
        if user in accounts:
            errors.append(f"{user!r}: listed twice")
            continue
        accounts[user] = Account(user, password, allowed)
    return accounts, errors


def config_error(enabled: bool, accounts: dict, account_errors: list[str],
                 tls: bool, certfile, keyfile) -> str | None:
    """Why the listener must not start, or None. Checked once, in run()."""
    if not enabled:
        return None
    if account_errors:
        return "STREAM_RAWOUT_USERS: " + "; ".join(account_errors)
    if not accounts:
        return ("STREAM_RAWOUT_ENABLE is set but STREAM_RAWOUT_USERS is empty - "
                "there is no anonymous access to a raw stream (it gives away the "
                "station's position)")
    if tls:
        missing = [str(p) for p in (certfile, keyfile) if not p or not p.is_file()]
        if missing:
            return ("STREAM_RAWOUT_TLS is on but the certificate is missing "
                    f"({', '.join(missing)}) - refusing to serve plain text instead")
    return None


# ==========================================================================
# Clients and the hub
# ==========================================================================
@dataclass(eq=False)
class _Client:
    user: str
    station: int
    peer: str
    ntrip: int  # 1 or 2
    writer: asyncio.StreamWriter
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    bytes_out: int = 0

    def send(self, raw: bytes) -> None:
        if self.ntrip == 2:
            # NTRIP 2 data is HTTP/1.1 chunked; one chunk per frame.
            self.writer.write(b"%X\r\n" % len(raw) + raw + b"\r\n")
        else:
            self.writer.write(raw)
        self.bytes_out += len(raw)

    def buffered(self) -> int:
        try:
            return self.writer.transport.get_write_buffer_size()
        except Exception:  # noqa: BLE001 - a dead transport has no buffer to speak of
            return 0

    def status(self) -> dict:
        return {
            "user": self.user, "station": self.station, "peer": self.peer,
            "ntrip": self.ntrip, "since": self.since.isoformat(),
            "bytes_out": self.bytes_out, "buffer": self.buffered(),
        }


@dataclass
class _StationStats:
    label: str
    bytes_out: int = 0
    dropped_slow: int = 0


class RawOutHub:
    """Who reads which station, and the fan-out itself."""

    def __init__(self, accounts: dict[str, Account], buffer_limit: int, max_clients: int):
        self.accounts = accounts
        self.buffer_limit = buffer_limit
        self.max_clients = max_clients
        self._clients: dict[int, set[_Client]] = {}
        self._stations: dict[int, _StationStats] = {}
        self.auth_failures = 0
        self._auth_logged: dict[str, float] = {}

    # -- stations ------------------------------------------------------------
    def note_station(self, station_id: int, label: str) -> None:
        """A station exists from its first identification this process on."""
        self._stations.setdefault(station_id, _StationStats(label))

    def stations_for(self, account: Account) -> list[tuple[int, str]]:
        return sorted((sid, st.label) for sid, st in self._stations.items()
                      if account.may_read(sid))

    def knows(self, station_id: int) -> bool:
        return station_id in self._stations

    # -- auth ----------------------------------------------------------------
    def authenticate(self, user: str | None, password: str | None) -> Account | None:
        account = self.accounts.get(user or "")
        # Compare even for an unknown user, so the answer takes as long either way.
        expected = account.password if account else "\0unknown-user"
        ok = hmac.compare_digest((password or "").encode(), expected.encode())
        return account if (account and ok) else None

    def note_auth_failure(self, peer_host: str, user: str | None) -> None:
        self.auth_failures += 1
        now = time.monotonic()
        if now - self._auth_logged.get(peer_host, -_AUTH_LOG_INTERVAL) >= _AUTH_LOG_INTERVAL:
            self._auth_logged[peer_host] = now
            logger.warning("rawout: failed login from %s (user %r)", peer_host, user)

    # -- clients -------------------------------------------------------------
    def client_count(self) -> int:
        return sum(len(c) for c in self._clients.values())

    def add(self, client: _Client) -> None:
        self._clients.setdefault(client.station, set()).add(client)
        logger.info("rawout: %s (%s, ntrip %d) reading station %s",
                    client.user, client.peer, client.ntrip, client.station)

    def remove(self, client: _Client) -> None:
        clients = self._clients.get(client.station)
        if clients is not None and client in clients:
            clients.discard(client)
            logger.info("rawout: %s (%s) left station %s after %d bytes",
                        client.user, client.peer, client.station, client.bytes_out)

    def publish(self, station_id: int, raw: bytes) -> None:
        """One frame to every reader of this station. Never blocks, never raises."""
        clients = self._clients.get(station_id)
        if not clients:
            return
        stats = self._stations.get(station_id)
        for client in list(clients):
            try:
                client.send(raw)
                if stats is not None:
                    stats.bytes_out += len(raw)
                buffered = client.buffered()
                if buffered > self.buffer_limit:
                    clients.discard(client)
                    if stats is not None:
                        stats.dropped_slow += 1
                    logger.warning(
                        "rawout: %s (%s) cannot keep up with station %s - %d bytes "
                        "buffered, cutting it off", client.user, client.peer,
                        station_id, buffered)
                    client.writer.transport.abort()
            except Exception as exc:  # noqa: BLE001 - one client's socket is its own problem
                clients.discard(client)
                logger.debug("rawout: dropping %s (%s): %s", client.user, client.peer, exc)
                try:
                    client.writer.transport.abort()
                except Exception:  # noqa: BLE001
                    pass

    def close_all(self) -> None:
        for clients in self._clients.values():
            for client in list(clients):
                try:
                    client.writer.transport.abort()
                except Exception:  # noqa: BLE001
                    pass
            clients.clear()

    def status(self) -> dict:
        return {
            "clients": [c.status() for cs in self._clients.values() for c in cs],
            "stations": {
                str(sid): {"label": st.label, "clients": len(self._clients.get(sid, ())),
                           "bytes_out": st.bytes_out, "dropped_slow": st.dropped_slow}
                for sid, st in sorted(self._stations.items())
            },
            "auth_failures": self.auth_failures,
        }


class RawOutSink(Sink):
    """One per station: the receiver's UBX and RTCM3, in arrival order, to the hub."""

    def __init__(self, hub: RawOutHub, station_id: int, label: str):
        self.hub = hub
        self.station_id = station_id
        hub.note_station(station_id, label)

    def on_ubx(self, raw: bytes, stamp, sysclk: bool) -> None:
        self.hub.publish(self.station_id, raw)

    def on_rtcm3(self, raw: bytes, stamp, sysclk: bool) -> None:
        self.hub.publish(self.station_id, raw)


# ==========================================================================
# The NTRIP side
# ==========================================================================
def sourcetable(stations: list[tuple[int, str]]) -> bytes:
    lines = [
        f"STR;{sid};{label};UBX+RTCM3;;0;GPS+GLO+GAL+BDS;wormhole;;0.00;0.00;0;0;"
        f"{SERVER_AGENT};none;B;N;0;"
        for sid, label in stations
    ]
    return ("".join(line + "\r\n" for line in lines) + "ENDSOURCETABLE\r\n").encode("ascii", "replace")


def _parse_basic(value: str | None) -> tuple[str | None, str | None]:
    if not value or not value[:6].lower() == "basic ":
        return None, None
    try:
        decoded = base64.b64decode(value[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None, None
    user, sep, password = decoded.partition(":")
    return (user, password) if sep else (None, None)


def _response(ntrip: int, code: int, reason: str, extra: str = "", body: bytes = b"") -> bytes:
    proto = "HTTP/1.1" if ntrip == 2 else "HTTP/1.0"
    head = f"{proto} {code} {reason}\r\nServer: {SERVER_AGENT}\r\n"
    if ntrip == 2:
        head += "Ntrip-Version: Ntrip/2.0\r\n"
    head += extra + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    return head.encode("ascii") + body


class RawOutServer:
    def __init__(self, hub: RawOutHub, host: str, port: int,
                 ssl_context: ssl.SSLContext | None = None):
        self.hub = hub
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> asyncio.base_events.Server:
        kwargs = {"limit": _HEADER_LIMIT}
        if self.ssl_context is not None:
            kwargs.update(ssl=self.ssl_context, ssl_handshake_timeout=_HEADER_TIMEOUT)
        self._server = await asyncio.start_server(self.handle, self.host, self.port, **kwargs)
        self.port = self._server.sockets[0].getsockname()[1]
        return self._server

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer_info = writer.get_extra_info("peername")
        peer_host = str(peer_info[0]) if peer_info else "?"
        peer = f"{peer_info[0]}:{peer_info[1]}" if peer_info else "?"
        client = None
        try:
            client = await self._handshake(reader, writer, peer_host, peer)
            if client is None:
                return
            # Nothing the client sends matters (some send GGA). Read only to
            # notice that it went away.
            while await reader.read(4096):
                pass
        except Exception as exc:  # noqa: BLE001 - one client must never touch the loop
            logger.debug("rawout: %s: %s", peer, exc)
        finally:
            if client is not None:
                self.hub.remove(client)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handshake(self, reader, writer, peer_host: str, peer: str) -> _Client | None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEADER_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ConnectionError, ValueError):
            return None

        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split()
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        ntrip = 2 if headers.get("ntrip-version", "").lower().startswith("ntrip/2") else 1

        if len(parts) != 3 or not parts[2].upper().startswith("HTTP/"):
            if ntrip == 2:
                writer.write(_response(2, 400, "Bad Request"))
            return None
        method, path = parts[0].upper(), parts[1]
        if method != "GET":
            if ntrip == 2:
                writer.write(_response(2, 405, "Method Not Allowed", "Allow: GET\r\n"))
            return None

        mount = path.split("?", 1)[0].strip("/")
        user, password = _parse_basic(headers.get("authorization"))
        account = self.hub.authenticate(user, password)
        if account is None:
            self.hub.note_auth_failure(peer_host, user)
            realm = f'WWW-Authenticate: Basic realm="/{mount}"\r\n'
            writer.write(_response(ntrip, 401, "Unauthorized", realm))
            return None

        table = sourcetable(self.hub.stations_for(account))
        station = int(mount) if mount.isdigit() else None
        # A station this account may not read does not exist for it: same answer
        # as an unknown mountpoint, so an account learns nothing about the others.
        if station is None or not self.hub.knows(station) or not account.may_read(station):
            if ntrip == 2 and mount:
                writer.write(_response(2, 404, "Not Found"))
            elif ntrip == 2:
                writer.write(_response(2, 200, "OK", "Content-Type: gnss/sourcetable\r\n", table))
            else:
                writer.write(
                    f"SOURCETABLE 200 OK\r\nServer: {SERVER_AGENT}\r\n"
                    f"Content-Type: text/plain\r\nContent-Length: {len(table)}\r\n\r\n"
                    .encode("ascii") + table)
            return None

        if self.hub.client_count() >= self.hub.max_clients:
            logger.warning("rawout: %s refused for station %s - %d clients is the limit",
                           peer, station, self.hub.max_clients)
            writer.write(_response(ntrip, 503, "Service Unavailable"))
            return None

        if ntrip == 2:
            writer.write(
                f"HTTP/1.1 200 OK\r\nServer: {SERVER_AGENT}\r\nNtrip-Version: Ntrip/2.0\r\n"
                "Content-Type: gnss/data\r\nTransfer-Encoding: chunked\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode("ascii"))
        else:
            # Exactly this line and nothing else: RTKLIB strips "ICY 200 OK\r\n"
            # and treats every byte after it as data.
            writer.write(b"ICY 200 OK\r\n")
        client = _Client(account.user, station, peer, ntrip, writer)
        self.hub.add(client)
        return client

    async def close(self) -> None:
        self.hub.close_all()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def make_ssl_context(certfile, keyfile) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(str(certfile), str(keyfile))
    return ctx
