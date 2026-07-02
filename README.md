# Wormhole

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

## Project Structure

```
wormhole/
├── server.py                        # FastAPI server
├── requirements.txt                 # Python dependencies
├── .env.example                     # Configuration template
├── generate-ssl.sh                  # SSL certificate generator
├── wormhole.service                 # systemd service template
├── server-deployment.md             # Server setup guide
├── test_upload.py                   # Upload test client
├── test_download.py                 # Download test client
├── test_chunk_upload.py             # In-process test for chunked-upload reassembly
└── data/
    ├── incoming/                    # Received uploads
    └── outgoing/                    # Files available for download
```

## License

MIT
