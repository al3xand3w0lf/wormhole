# Wormhole

Two **independent** servers for two ways an IoT device can deliver data. They share
this repo, the `.env` and the `data/` tree, but run as **separate processes / systemd
services** and do not import each other.

| Mode | What the device does | Server | Ports | Entry point |
|---|---|---|---|---|
| **Batch** | uploads finished files over HTTP | file server | 8000 | `server.py` |
| **Streaming** | live TCP byte stream | stream receiver | 9000 (TCP) + 9001 (admin) | `streaming_server.py` |

Run one, or both. → [Streaming Server](#streaming-server) (jump to the second half)

---

# Batch File Server (`server.py`)

HTTP(S) file server for IoT devices.

Receives and serves files via raw HTTP POST/GET — no multipart/form-data required.

## Features

- Raw binary upload/download
- **Chunked upload with server-side reassembly** — a device whose file is too large for its modem buffer splits it into pieces; the server writes each at its byte offset and reassembles the original file (see below)
- API key authentication (`X-API-Key` header)
- Optional HTTPS with self-signed or Let's Encrypt certificates
- File collision handling (automatic rename)
- Filename sanitization and extension blocking
- Per-request IDs (`X-Request-ID` header, included in every log line)
- Separate rotating logs: operational (`server.log`) and access (`server.access.log`), also on stdout
- Upload chunk timeout — drops stalled transfers with HTTP 408
- Simple HTTP API usable with any client (`curl`, scripts, IoT devices)

## API Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/health` | Health check | No |
| `POST` | `/modem/upload?device_id=X&filename=Y` | Upload file (raw binary body) | Yes |
| `GET` | `/modem/download/{filename}` | Download file (octet-stream) | Yes |
| `GET` | `/modem/download` | List available downloads | Yes |
| `GET` | `/uploads` | List received uploads | Yes |

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # set API_KEY!
python3 server.py
```

### Options

```bash
python3 server.py --port 8080    # custom port
python3 server.py --no-ssl       # disable HTTPS
```

### SSL Certificates

```bash
bash generate-ssl.sh             # generates cert.pem + key.pem
```

## Configuration (.env)

See [.env.example](.env.example) for all options:

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | `changeme` | API key for authentication |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Server port |
| `UPLOAD_DIR` | `./data/incoming` | Directory for received files |
| `DOWNLOAD_DIR` | `./data/outgoing` | Directory for files to serve |
| `SSL_CERTFILE` | `./cert.pem` | SSL certificate path |
| `SSL_KEYFILE` | `./key.pem` | SSL private key path |
| `MAX_FILE_SIZE` | `52428800` | Max size per single request / chunk (50 MB) |
| `MAX_ASSEMBLED_SIZE` | `524288000` | Max size of a chunk-reassembled file (500 MB) |
| `PARTIAL_MAX_AGE_H` | `48` | Age (hours) after which abandoned `.partial` files are swept at startup |
| `UPLOAD_CHUNK_TIMEOUT` | `30` | Per-chunk read timeout in seconds (HTTP 408 on stall) |
| `LOG_FILE` | `./server.log` | Operational log (events + 4xx/5xx on real endpoints) |
| `ACCESS_LOG_FILE` | `./server.access.log` | Access log (one line per request) |
| `LOG_MAX_BYTES` | `10485760` | Max bytes per log file before rotation (10 MB) |
| `LOG_BACKUP_COUNT` | `5` | Number of rotated log files to keep |

## Usage

Authentication is via the `X-API-Key` header on every endpoint except `/health`.
The examples below use `curl`; any HTTP client — or an IoT device capable of raw
HTTP POST/GET — works the same way.

### Upload a file (raw binary body)

```bash
curl -X POST "https://<host>:<port>/modem/upload?device_id=device01&filename=data.bin" \
     -H "X-API-Key: <key>" \
     -H "Content-Type: application/octet-stream" \
     --data-binary @data.bin
```

### Chunked upload (large files)

A device whose file is larger than its modem's staging buffer splits the file
into pieces and adds four query params to each `POST /modem/upload`:

| Param | Meaning |
|-------|---------|
| `chunk_index` | 0-based index of this chunk |
| `chunk_count` | total number of chunks |
| `offset` | byte offset of this chunk in the assembled file |
| `total_size` | full assembled file size in bytes |

The server writes each chunk's raw body at `offset` into `<filename>.<device>.partial`.
A **non-final** chunk is answered **`202 Accepted`**; the **final** chunk
(`chunk_index == chunk_count-1`) is verified against `total_size` and the partial
is atomically renamed to the target filename (**`201 Created`**). Chunk 0 truncates
the partial and every chunk seeks to its offset, so a full retry or a re-sent chunk
is idempotent. A request **without** these params is stored whole, exactly as before —
so this is fully backward compatible.

```bash
# chunk 0 of 3 -> 202
curl -X POST "https://<host>:<port>/modem/upload?device_id=device01&filename=big.ubx&chunk_index=0&chunk_count=3&offset=0&total_size=2500000" \
     -H "X-API-Key: <key>" --data-binary @big.part0
# ... chunk 1 -> 202 ...
# chunk 2 of 3 (final) -> 201, big.ubx reassembled
curl -X POST "https://<host>:<port>/modem/upload?device_id=device01&filename=big.ubx&chunk_index=2&chunk_count=3&offset=2000000&total_size=2500000" \
     -H "X-API-Key: <key>" --data-binary @big.part2
```

A chunk-unaware server ignores the unknown params and returns `200/201` for the
first chunk; a device can detect that (`200/201` on a non-final chunk) and fall
back to a single whole-file upload.

### Download a file

```bash
curl "https://<host>:<port>/modem/download/config.bin" \
     -H "X-API-Key: <key>" \
     -o config.bin
```

### List files

```bash
curl "https://<host>:<port>/modem/download" -H "X-API-Key: <key>"   # available downloads
curl "https://<host>:<port>/uploads"        -H "X-API-Key: <key>"   # received uploads
```

> For plain HTTP (no TLS), use `http://` and start the server with `--no-ssl`.

## Testing

Two self-contained clients (Python standard library only, no `.env`) let you
test a running server from any machine. Edit `SERVER_URL`, `API_KEY` and
`VERIFY_TLS` at the top of each script, then:

```bash
python test_upload.py              # upload a generated test file
python test_upload.py --file mydata.bin
python test_upload.py --list       # also list the server's uploads

python test_download.py            # list available downloads
python test_download.py --filename config.bin --out ./config.bin
```

TLS verification is off by default (servers run over plain http); set
`VERIFY_TLS = True` in the script for a server with a valid HTTPS certificate.

The chunked-upload reassembly has its own in-process test (runs the app via
FastAPI's `TestClient`, no network or SSL needed):

```bash
pip install fastapi aiofiles python-dotenv httpx uvicorn
python test_chunk_upload.py        # 202/201 contract, byte-identical reassembly, idempotency, error cases
```

---

# Streaming Server

`streaming_server.py` — receives a **live TCP byte stream** from devices instead of
finished file uploads.

The device is a **thin, framed pipe**: it tees a raw binary stream (here: u-blox GNSS
data — UBX + RTCM3) straight off its receiver and interleaves its own private frames
(sensor readings, identification, heartbeat, CLI) over **one persistent TCP socket per
station**. All protocol intelligence lives on the server.

```bash
pip install -r requirements-dev.txt
cp .env.example .env          # set API_KEY and STREAM_CLI_SECRET
python3 streaming_server.py   # TCP :9000 (data) + HTTP :9001 (admin)
pytest                        # 238 tests
```

## What it does

- **Demuxes** the byte stream: UBX → `.ubx`, RTCM3 → `.rtcm3`, private frames → sensor
  CSVs / CLI / identification.
- **Records** everything to per-station files, rotated hourly on the **GNSS clock**.
- **Captures the raw stream** byte-exactly, so any session can be replayed offline.
- **Sends CLI commands** to a device and returns its response (admin API).

## Output layout

```
data/incoming_stream/<station>/
    ubx/       <station>_ubx_YYYYMMDD_HH.ubx        hourly, GNSS-time hour
    rtcm3/     <station>_rtcm3_YYYYMMDD_HH.rtcm3    hourly
    sensors/   <station>_<stream>_YYYYMMDD.csv      daily
    raw/       <station>_raw_YYYYMMDD_HH.bin        byte-exact capture (replay.py)
    cli/       <station>_cli_YYYYMMDD.log
```

`<station>` is the station's **label**. A device that sends its own name in IDENT
(`station_name`, e.g. `A001`) is archived as `A001_2001` — name *and* id, in the
directory and in every file name inside it, so a bare file name says which site it
belongs to. The id stays in the label because the name is free text, editable in the
field and not guaranteed unique, while everything runtime-side (caster mountpoint,
rover subscription, admin API) is keyed on the id. A device that sends no name is
archived as plain `2001`, exactly as before. See `streaming/stationdir.py`.

When a station that already has a bare-id directory starts sending a name, that
directory is renamed onto the label once, so its history and its new data stay in one
place. Files already written keep their old names.

CSV columns: `rtc_unix, gps_iso, utc_iso, leap_s, <values…>`

Rotation happens on the **GNSS hour change**, and only ever on a **frame boundary** —
a message split across two files would be unparseable in *both*. Before the first GNSS
fix the server clock is used and the filename says so (`_sysclk` suffix).

## ⚠️ GPS time vs UTC

GPS time runs ahead of UTC by the accumulated leap seconds (currently 18 s). A device
that derives its calendar straight from the GPS epoch — as the reference firmware does —
therefore timestamps everything in **GPS time, not UTC**, even though `leapS` is
available in `UBX-RXM-RAWX`.

This server **reproduces the device's convention deliberately**, so that recorded file
hour boundaries line up with the device's own. It records `leapS` and adds a `utc_iso`
column to the sensor CSVs, so true UTC is always available.

If your device already corrects for leap seconds, adjust
`streaming/gpstime.py::gps_to_datetime()` — it is the single place this decision lives,
and `tests/test_gpstime.py` pins it.

## Admin / CLI API (`:9001`, `X-API-Key`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (no auth) |
| `GET` | `/stream/stations` | Connected stations: GNSS clock, frame counters, resyncs, RTCM3 type histogram |
| `GET` | `/stream/rover` | Correction downlink counters + auto-discovery candidates |
| `GET` | `/stream/caster` | Bundled-caster mountpoints (which were auto-provisioned, their positions, the caster's pid) + configured extra targets |
| `POST`/`DELETE` | `/stream/rover/subscribe` | Manually add/remove a rover on a base's router, live, no restart |
| `POST` | `/stream/rover/reload` | Re-read `STREAM_ROVER_BASES` from `.env` and apply the diff live |
| `POST` | `/stream/{station_id}/cli` | Send a CLI command, get the reassembled response |

```bash
curl -H "X-API-Key: <key>" http://<host>:9001/stream/stations

curl -X POST http://<host>:9001/stream/1001/cli \
     -H "X-API-Key: <key>" -H "Content-Type: application/json" \
     -d '{"cmd": "sysinfo"}'
```

`/stream/stations` includes an **RTCM3 message-type histogram** — the quickest way to
confirm an RTK base is really emitting `1005` + MSM7 + `1230`:

```json
"rtcm3_types": {"1005": 42, "1077": 42, "1230": 42}
```

### Interactive terminal

`stream_cli/stream_cli.py` is a REPL and one-shot client for the endpoint above —
Python standard library only, and no configuration on the server host: it reads
`API_KEY` and the admin port out of the repo `.env` itself.

```bash
python stream_cli/stream_cli.py --list             # connected stations
python stream_cli/stream_cli.py --list-all         # ... plus the ones only remembered
python stream_cli/stream_cli.py --station 1001     # REPL: type sysinfo, whoami, ...
python stream_cli/stream_cli.py --station 1001 sysinfo   # one-shot

python stream_cli/stream_menu.py                   # menu: pick station, pick command
```

`stream_menu.py` is a menu in front of the same endpoint for operators who would
rather not type: it lists the connected stations, then the device commands — read
straight out of the allowlist table in `stream_cli_commands.md`, so the doc is the
command list — and `t` hands off to the REPL above.

It holds no frame logic and never talks to a device directly — the server owns the
secret, the response reassembly and the disconnect/reconnect transfer dance. Because
the admin port binds loopback, reaching it from another machine means an SSH tunnel.
Full reference, including the device allowlist: `stream_cli/stream_cli_commands.md`.

### CLI security

Two independent layers:
1. The **device** enforces a positive allowlist. Nothing else is executed, whatever the
   server sends.
2. A **pre-shared token**: `STREAM_CLI_SECRET` must match the token configured on the
   device, which rejects anything else.

The stream itself is **not encrypted** (no TLS yet) — the token authenticates, it does
not conceal.

### Commands that pause the stream

A file-transfer command (e.g. a firmware or config download over the same modem) makes
the device answer **twice**: it acks, **closes the socket**, runs the transfer, then
**reconnects** and only *then* sends the buffered output. The server keeps the pending
request alive across that disconnect — sessions are keyed by station id, not by
connection — so the caller gets the real result rather than the ack. Use
`STREAM_CLI_TRANSFER_TIMEOUT` (default 600 s).

### RTK rover correction downlink

A base already in the fleet (or an external NTRIP caster, via `STREAM_NTRIP_*`) can
feed rovers RTCM3 corrections over the same TCP socket — no separate radio link.
`STREAM_ROVER_BASES` pins specific base:rover pairs by hand; independently,
`STREAM_ROVER_AUTO_ENABLE` (on by default) lets a station that identifies itself as
a rover in its IDENT subscribe to its nearest trusted base automatically, live, no
restart, no config edit — only base station ids listed in
`STREAM_ROVER_AUTO_BASE_STATIONS` (or already a `STREAM_ROVER_BASES` key) are
trusted as a correction source; a bare claim on the wire is not enough. See
`streaming/rover.py` and `streaming/rover_discovery.py`.

## Robustness

A device can damage the stream — dropping bytes when its buffer fills, or tearing a
frame in two if a second producer writes between the chunks of a chunked send. The
framer **resyncs byte-by-byte** instead of desyncing, so a single bad frame costs one
frame and not the rest of the session. `resync_events` and `garbage_bytes` are exposed
per station in the admin API.

**With a healthy device both counters stay at 0.** Treat any sustained rise as a defect
to investigate, not as background noise. A garbage byte does not mean data was *lost* —
it means the framer could not *place* it, which is a different failure with a different
fix. Keep a raw capture (`STREAM_RAW_CAPTURE=true`) so the difference can be told apart
after the fact.

## Replay

```bash
python3 replay.py data/incoming_stream/1001/raw/ --out ./replayed
```

Feeds a recorded capture back through the **same** framer, router and sinks — so
decoders can be tested and history reprocessed without any hardware. Output is
byte-identical to the live run.

## Testing without hardware

`fake_device.py` emulates a device end to end — identification, GNSS frames, sensor
frames, and the full CLI flow including the pause/reconnect transfer dance:

```bash
python3 streaming_server.py &
python3 fake_device.py --secret <token> --duration 60
curl -H "X-API-Key: <key>" http://127.0.0.1:9001/stream/stations
```

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `STREAM_HOST` | `0.0.0.0` | TCP bind address |
| `STREAM_PORT` | `9000` | Device data port |
| `STREAM_ADMIN_PORT` | `9001` | Admin/CLI HTTP port — **do not expose publicly** |
| `STREAM_DIR` | `./data/incoming_stream` | Output root |
| `STREAM_CLI_SECRET` | *(empty)* | Pre-shared CLI token — **must match the device** |
| `STREAM_RAW_CAPTURE` | `true` | Record the raw byte stream |
| `STREAM_RAW_MAX_AGE_H` | `168` | Prune raw captures after N hours (7 days) |
| `STREAM_IDLE_TIMEOUT` | `180` | Drop a socket after N seconds of silence |
| `STREAM_CLI_TIMEOUT` | `60` | Normal CLI command timeout |
| `STREAM_CLI_TRANSFER_TIMEOUT` | `600` | Timeout for stream-pausing transfer commands |
| `STREAM_PRE_IDENT_CAP` | `8192` | Bytes allowed before a connection must identify itself |
| `STREAM_ROVER_BASES` | *(empty)* | In-fleet routing `base:rover[+rover...][,base:rover...]`; each base feeds its rovers in-process, no external caster. Empty turns in-fleet routing off |
| `STREAM_ROVER_STATIONS` | *(empty)* | Station ids that receive RTCM3 corrections from the **external** NTRIP source below. Empty turns the external downlink off |
| `STREAM_ROVER_QUEUE` | `24` | Per-rover queue depth in whole frames; the oldest is dropped on overflow |
| `STREAM_ROVER_RTCM_TYPES` | *(empty)* | Allowlist of forwarded RTCM3 message types; empty forwards everything. For a link that cannot carry the full stream — see `streaming/rover.py` |
| `STREAM_ROVER_AUTO_ENABLE` | `true` | Auto-subscribe role=rover stations to their nearest trusted base |
| `STREAM_ROVER_AUTO_BASE_STATIONS` | *(empty)* | Station ids trusted as a correction source when they identify as role=base |
| `STREAM_ROVER_MAX_BASELINE_KM` | `40` | Beyond this, a correction can't help — the rover is left unsubscribed |
| `STREAM_ROVER_SWITCH_MARGIN_KM` | `5` | Hysteresis: a rover only switches base if the nearer one wins by more than this |
| `STREAM_NTRIP_HOST` / `_PORT` | `rtk2go.com` / `2101` | Correction source caster |
| `STREAM_NTRIP_MOUNT` | *(empty)* | Source mountpoint; empty means rovers are configured without a source yet |
| `STREAM_NTRIP_USER` / `_PASS` | *(empty)* / `none` | Source caster credentials |
| `STREAM_CASTER_ENABLE` | `false` | Push demuxed RTCM3 out to an NTRIP caster (the reverse direction from `STREAM_NTRIP_*` above) |
| `STREAM_CASTER_HOST` / `_PORT` | `127.0.0.1` / `2101` | Target caster — defaults to the one bundled in `caster/` |
| `STREAM_CASTER_PASSWORDS` | *(empty)* | `station:password[,station:password...]` — a station missing here gets no caster push |
| `STREAM_CASTER_STATIONS` | *(empty)* | Stations that have a mountpoint — `caster/generate_config.py` provisions from it, the server renders the sourcetable from it |
| `STREAM_CASTER_AUTO_ENABLE` | `false` | Provision a mountpoint on the **bundled** caster automatically when a station identifies with `role=base` — no `.env` edit, no restart |
| `STREAM_CASTER_ETC_DIR` | `./caster/millipede-caster/etc` | Where the bundled caster's config lives; only the auto-provisioner writes there |
| `STREAM_CASTER_TARGETS` | *(empty)* | Further casters to push the same stations to, by name — each configured by `STREAM_CASTER_<NAME>_{HOST,PORT,PASSWORDS,ENABLE}`. Additive to the bundled caster above |

`API_KEY` is shared with the batch server.

## Implementation notes

UBX and RTCM3 message definitions, parsing, checksum and CRC-24Q come from
**`pyubx2` / `pyrtcm`** — none of that is hand-rolled. Only three things are ours, each
for a stated reason:

1. **The framer** (`streaming/framer.py`) — `pyubx2`'s `UBXReader` reads from a
   *blocking* stream, but this server is asyncio; we also need byte-exact raw frames for
   the recordings, and our own resync counters.
2. **The private frame class** (`streaming/frames.py`) — the device's own protocol,
   carried inside a UBX envelope (private class `0xF0`) so it rides the same sync-byte
   scan and is checksum-protected.
3. **The GPS→calendar conversion** (`streaming/gpstime.py`) — it must mirror the
   device's convention, so `pyubx2`'s UTC helpers would be *wrong* here.

### Adapting to your own device

| You want to… | Change |
|---|---|
| add a sensor type | a new id + a `SENSOR_SPECS` entry in `streaming/frames.py` |
| change the time convention | `streaming/gpstime.py::gps_to_datetime()` |
| change the file layout | `streaming/sinks.py` |
| forward data somewhere (NTRIP caster, message bus, live fan-out) | add a `Sink` subclass — the framer and routing stay untouched |

## NTRIP caster

Two independent, additive pieces (the archive sink keeps recording everything either
way): `streaming/ntrip.py`'s `NtripCasterSink` pushes a station's demuxed RTCM3 live
to any NTRIP caster's mountpoint (`STREAM_CASTER_*` above), and `caster/` bundles an
actual caster (Millipede) with this repo so there's somewhere to push to out of the
box — `caster/setup.sh` builds it and provisions one mountpoint per configured
station. See `caster/README.md`. A rover can then get corrections either directly
over the streaming TCP socket, or indirectly by pulling from this caster as a
standard NTRIP client.

`tools/ntrip_relay.py` relays an existing NTRIP mountpoint into one or more casters,
which is how you exercise a fresh caster with a real correction stream before you have
a station of your own pushing into it.

**A base provisions its own mountpoint.** With `STREAM_CASTER_AUTO_ENABLE=true`, a
station identifying with `role=base` gets a mountpoint on the **bundled** caster the
moment it connects: password generated, `sourcetable.dat`/`source.auth` rewritten,
caster SIGHUPed, `NtripCasterSink` attached to the *live* session, and
`STREAM_CASTER_{STATIONS,PASSWORDS,ENABLE}` written back into `.env`. Its sourcetable
position (Millipede's NEAR routing) fills in from its own first RTCM 1005 instead of
from the archive. Reconfiguring a device to be a base is then the whole procedure —
no `.env` edit, no restart, no second `generate_config.py` run.

> **The role byte alone is the gate here, and that is deliberate.** `rover_discovery.py`
> demands `STREAM_ROVER_AUTO_BASE_STATIONS` on top of the same claim, and the two must
> not be collapsed into one decision: a trusted base gets pushed **into our own
> rovers**, which compute a position from whatever arrives, while a mountpoint only
> makes that station's RTCM3 **retrievable under its own name**, chosen deliberately by
> a client. Only the weaker consequence runs off a bare wire claim. Off by default.
>
> Only ever the bundled caster — `STREAM_CASTER_TARGETS` are other people's casters
> whose config this server does not own, and stay hand-provisioned.
>
> **Both writers of those two files render through `streaming/caster_config.py`.**
> `caster/generate_config.py` (setup time) and `CasterAutoProvision` (runtime) must
> stay byte-identical for the same station set, or each run silently drops the other's
> stations. If you touch the sourcetable format, touch it there.
>
> No automatic *removal*: a station that stops being a base keeps its mountpoint.
> Revoking on a role change would tear down a working publication on every firmware
> hiccup.

An operator's own caster accounts and mountpoint credentials are still deployment data
and belong in `.env`, never in the repo.

## Not implemented

- **Live data fan-out.**
- **TLS on the stream socket.**

The sink abstraction exists so it can be added without touching the framer or
the routing.

---

## Project Structure

```
wormhole/
├── server.py                        # Batch: FastAPI file server
├── downloads.py                     # Shared download dir, used by server.py + streaming/filetransfer.py
├── streaming_server.py              # Streaming: entry point
├── streaming/                       # Streaming: framer, frames, filetransfer, geo,
│                                    #   gpstime, pipeline, rover, rover_discovery,
│                                    #   sinks, station, server, config
├── replay.py                        # Streaming: replay a raw capture
├── fake_device.py                   # Streaming: device emulator
├── caster/                          # Bundled NTRIP caster (Millipede): setup.sh, generate_config.py
├── tools/                           # ntrip_relay.py — relay a mountpoint into one or more casters
├── stream_cli/                      # Interactive terminal over the admin API (stdlib only)
├── tests/                           # Streaming: pytest suite
├── pytest.ini
├── requirements.txt                 # Python dependencies
├── requirements-dev.txt             # + pytest
├── .env.example                     # Configuration template (both servers)
├── generate-ssl.sh                  # SSL certificate generator
├── wormhole.service                 # systemd service (batch)
├── wormhole-streaming.service       # systemd service (streaming)
├── server-deployment.md             # Server setup guide
├── test_upload.py                   # Batch: upload test client
├── test_download.py                 # Batch: download test client
├── test_chunk_upload.py             # Batch: in-process test for chunked-upload reassembly
└── data/
    ├── incoming/                    # Batch: received uploads
    ├── outgoing/                    # Batch: files available for download
    └── incoming_stream/             # Streaming: per-station demuxed output
```

## License

MIT
