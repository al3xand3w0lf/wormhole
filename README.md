# Wormhole

HTTP(S) file server for Quectel LTE modems (EC2x, EG9x, EM05).

Receives and serves files via raw HTTP — optimized for the Quectel AT command HTTP stack (`AT+QHTTPPOST` / `AT+QHTTPGET`), no multipart/form-data required.

## Features

- Raw binary upload/download (compatible with Quectel HTTP AT commands)
- API key authentication (`X-API-Key` header)
- Optional HTTPS with self-signed or Let's Encrypt certificates
- File collision handling (automatic rename)
- Filename sanitization and extension blocking
- Request logging to file and stdout

## API Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/health` | Health check | No |
| `POST` | `/modem/upload?device_id=X&filename=Y` | Upload file (raw binary body) | Yes |
| `GET` | `/modem/download/{filename}` | Download file (octet-stream) | Yes |
| `GET` | `/modem/download` | List available downloads | Yes |
| `GET` | `/modem/uploads` | List received uploads | Yes |

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
| `LOG_FILE` | `./server.log` | Log file path |

## Quectel Modem Usage

### Upload (Modem → Server)

```
AT+QHTTPCFG="contextid",1
AT+QHTTPCFG="requestheader",1
AT+QHTTPURL=<url_len>
http://<host>:<port>/modem/upload?device_id=10000002&filename=data.ubx

AT+QHTTPPOST=<filesize>,120,80
POST /modem/upload?device_id=10000002&filename=data.ubx HTTP/1.1
Host: <host>:<port>
X-API-Key: <key>
Content-Type: application/octet-stream
Content-Length: <filesize>

<binary data>
```

### Download (Server → Modem)

```
AT+QHTTPCFG="requestheader",1
AT+QHTTPURL=<url_len>
http://<host>:<port>/modem/download/config.bin

AT+QHTTPGET=80,<header_len>
GET /modem/download/config.bin HTTP/1.1
Host: <host>:<port>
X-API-Key: <key>

AT+QHTTPREADFILE="UFS:config.bin",80
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
├── modem-client-implementation.md   # IoT / AT command guide
└── data/
    ├── incoming/                    # Received uploads
    └── outgoing/                    # Files available for download
```

## License

MIT
