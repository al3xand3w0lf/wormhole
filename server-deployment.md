# Wormhole Server — Deployment Guide

Step-by-step guide to deploying the Wormhole server on a fresh Debian/Ubuntu VPS.

## Prerequisites

- Root access on the server
- Debian 12/13 or Ubuntu 22.04+

---

## Step 1: Update system & install packages

```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git curl openssl
```

Verify:

```bash
python3 --version
git --version
```

---

## Step 2: Clone the repository

```bash
git clone https://github.com/al3xand3w0lf/wormhole.git <INSTALL_DIR>
cd <INSTALL_DIR>
```

> Replace `<INSTALL_DIR>` with your target path, e.g. `/opt/wormhole`.

---

## Step 3: Set up Python environment

```bash
cd <INSTALL_DIR>
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Step 4: Directories & configuration

```bash
mkdir -p data/incoming data/outgoing
cp .env.example .env
nano .env
```

Set at minimum a secure `API_KEY`:

```
API_KEY=your-secret-key
PORT=8000
HOST=0.0.0.0
```

---

## Step 5: Test the server

```bash
source venv/bin/activate
python3 server.py --no-ssl
```

In a second terminal:

```bash
curl http://localhost:8000/health
```

Expected response: `{"status":"ok",...}`

---

## Step 6: Test from outside

```bash
curl http://<server-ip>:8000/health
```

> Make sure port 8000 TCP inbound is open in your firewall / cloud panel.

---

## Step 7: Set up systemd service (autostart)

Copy the service file and replace `<INSTALL_DIR>` with your actual path:

```bash
sed 's|<INSTALL_DIR>|/opt/wormhole|g' wormhole.service > /etc/systemd/system/wormhole.service
```

Enable and start:

```bash
systemctl daemon-reload
systemctl enable wormhole
systemctl start wormhole
systemctl status wormhole
```

Follow logs:

```bash
journalctl -u wormhole -f
```

---

## Step 8 (Optional): HTTPS

### With a domain — Let's Encrypt

```bash
apt install -y certbot
certbot certonly --standalone -d your-domain.com
```

In `.env`:

```
SSL_CERTFILE=/etc/letsencrypt/live/your-domain.com/fullchain.pem
SSL_KEYFILE=/etc/letsencrypt/live/your-domain.com/privkey.pem
```

Start without `--no-ssl`:

```bash
python3 server.py
```

### Without a domain — self-signed certificate

```bash
bash generate-ssl.sh
```

In `.env`:

```
SSL_CERTFILE=./cert.pem
SSL_KEYFILE=./key.pem
```

---

## Updates

```bash
cd <INSTALL_DIR>
source venv/bin/activate
git pull
pip install -r requirements.txt
systemctl restart wormhole
systemctl restart wormhole-streaming   # if the streaming server is running
```

---

# Streaming Server

The streaming server (`streaming_server.py`) is a **separate process** alongside the
batch file server. Both share the repo, the venv and the `.env`, but run as separate
systemd services — a crash or load spike in one does not take the other down.

| | Port | Protocol |
|---|---|---|
| Batch file server (`server.py`) | 8000 | HTTP(S) |
| Streaming data plane | **9000** | raw TCP (devices) |
| Streaming admin / CLI | **9001** | HTTP (`X-API-Key`) |

## Step S1: Configuration

Add to `.env` (see `.env.example` for all options):

```
STREAM_PORT=9000
STREAM_ADMIN_PORT=9001
STREAM_DIR=./data/incoming_stream

# MUST match the token configured on the device!
STREAM_CLI_SECRET=your-secret-token
```

> The streaming server needs `pyubx2` (which pulls in `pyrtcm`). It is listed in
> `requirements.txt`, so re-run `pip install -r requirements.txt` after updating.

## Step S2: Firewall

Open port **9000 TCP** inbound for the devices.

Port **9001** is the admin API — **do not expose it publicly**. Keep it bound to the
internal interface, or reach it through an SSH tunnel:

```bash
ssh -L 9001:localhost:9001 root@<server-ip>
curl -H "X-API-Key: <key>" http://localhost:9001/stream/stations
```

## Step S3: Test

```bash
source venv/bin/activate
python3 streaming_server.py
```

In a second terminal — no real hardware needed, use the device emulator:

```bash
source venv/bin/activate
python3 fake_device.py --secret your-secret-token --duration 30
curl -H "X-API-Key: <key>" http://localhost:9001/stream/stations
```

Expected: one station with `"connected": true`, a populated `gps_time`, and frame
counters going up.

## Step S4: systemd service

```bash
nano /etc/systemd/system/wormhole-streaming.service
```

```ini
[Unit]
Description=Wormhole Streaming Server (TCP data plane + admin API)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=<INSTALL_DIR>
ExecStart=<INSTALL_DIR>/venv/bin/python3 streaming_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable wormhole-streaming
systemctl start wormhole-streaming
systemctl status wormhole-streaming
journalctl -u wormhole-streaming -f
```

## Step S5: Point the device at the server

The device needs the server IP, port **9000**, a unique station id, and the **same**
CLI token as `STREAM_CLI_SECRET`.

On the reference firmware this lives in `CONFIG.TXT` on the device's SD card:

```
operation_mode = 1
streaming_server_ip = <server-ip>
streaming_server_port = 9000
streaming_station_id = 1001
streaming_cli_secret = your-secret-token
```

`operation_mode = 1` switches the batch upload **off** — the two modes are mutually
exclusive.

## Disk space

Rough order of magnitude per station: **~30 MB/h** of `.ubx`, plus RTCM3, plus the
**raw capture** (roughly the sum of both again, since it records everything a second
time byte-for-byte).

The raw capture is pruned at startup (`STREAM_RAW_MAX_AGE_H`, default 7 days). For
long-term operation lower it, or turn it off with `STREAM_RAW_CAPTURE=false` — it is
primarily a debugging and replay tool.

## Operating

```bash
# Who is connected? Incl. GNSS clock, frame counters, resyncs, RTCM3 types
curl -H "X-API-Key: <key>" http://localhost:9001/stream/stations

# Send a command to a device
curl -X POST http://localhost:9001/stream/1001/cli \
     -H "X-API-Key: <key>" -H "Content-Type: application/json" \
     -d '{"cmd": "sysinfo"}'
```

`resync_events` / `garbage_bytes` staying at 0 means clean reception. Rising values mean
packet loss, or the device's send buffer overflowing — the latter is by design
(best-effort streaming), so only persistently high values are a warning sign.
