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
```
