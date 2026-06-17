# Wormhole

HTTP(S) file server for IoT devices.

Receives and serves files via raw HTTP POST/GET — no multipart/form-data required.

## Features

- Raw binary upload/download
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
| `MAX_FILE_SIZE` | `52428800` | Max upload size (50 MB) |
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

Two small stdlib-only clients are included to exercise a running server:

```bash
# Upload a generated test file (and list the server's uploads)
python test_upload.py --url http://127.0.0.1:8000 --api-key <key> --list

# Upload an existing file
python test_upload.py --file mydata.bin --api-key <key>

# List downloadable files, then fetch one
python test_download.py --api-key <key>
python test_download.py --filename config.bin --api-key <key> --out ./config.bin
```

Add `--insecure` for HTTPS with a self-signed certificate. Both scripts resolve
the API key from `--api-key`, the `API_KEY` env var, or a local `.env`.

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
└── data/
    ├── incoming/                    # Received uploads
    └── outgoing/                    # Files available for download
```

## License

MIT
