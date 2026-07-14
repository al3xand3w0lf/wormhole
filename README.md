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
pytest                        # 135 tests
```

## What it does

- **Demuxes** the byte stream: UBX → `.ubx`, RTCM3 → `.rtcm3`, private frames → sensor
  CSVs / CLI / identification.
- **Records** everything to per-station files, rotated hourly on the **GNSS clock**.
- **Captures the raw stream** byte-exactly, so any session can be replayed offline.
- **Sends CLI commands** to a device and returns its response (admin API).

## Output layout

```
data/incoming_stream/<stationId>/
    ubx/       <station>_ubx_YYYYMMDD_HH.ubx        hourly, GNSS-time hour
    rtcm3/     <station>_rtcm3_YYYYMMDD_HH.rtcm3    hourly
    sensors/   <station>_<stream>_YYYYMMDD.csv      daily
    raw/       <station>_raw_YYYYMMDD_HH.bin        byte-exact capture (replay.py)
    cli/       <station>_cli_YYYYMMDD.log
```

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

## Not implemented

- **NTRIP caster** — RTCM3 is recorded but not forwarded.
- **Live data fan-out.**
- **TLS on the stream socket.**

The sink abstraction exists so the first two can be added without touching the framer or
the routing.

---

## Project Structure

```
wormhole/
├── server.py                        # Batch: FastAPI file server
├── streaming_server.py              # Streaming: entry point
├── streaming/                       # Streaming: framer, frames, gpstime,
│                                    #   sinks, pipeline, station, server, config
├── replay.py                        # Streaming: replay a raw capture
├── fake_device.py                   # Streaming: device emulator
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
