# Wormhole Groundstation — Setup Guide

Builds a complete groundstation from an empty machine: the batch file server, the
streaming receiver, and the NTRIP caster bundled in `caster/`. Debian/Ubuntu.

Every step below was executed end to end on a fresh clone before this guide was
written; the verification commands are the ones that were actually run, not
illustrations. Follow it in order — each step ends with a check, so a mistake is
caught where it was made instead of three steps later.

## What you end up with

Three independent processes. They share the repo, the venv, the `.env` and the
`data/` tree, but they run as separate services and do not import each other — one
crashing or being restarted does not touch the others.

| Process | Default port | Role |
|---|---|---|
| `server.py` | 8000 (HTTP) | Batch mode: devices upload finished files |
| `streaming_server.py` | 9000 (TCP) | Streaming mode: live byte stream, one socket per station |
| ↳ its admin API | 9001 (HTTP) | Status + remote CLI — **loopback only**, never public |
| `caster/` (Millipede) | 2101 (TCP) | NTRIP caster: rovers pull the corrections a base pushed in |

A rover can take corrections either **directly over the streaming socket** (Direct
Streaming Mode) or **as a standard NTRIP client** from the caster. The caster is
optional — skip step 6 and everything else still works.

Pick the ports as one contiguous block per instance, named after the streaming
port, and keep the batch port next to it rather than in an unrelated range. See
"Several instances on one host" below.

---

## 1. Packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl
```

Only if you want the bundled caster (step 6) — `caster/setup.sh` installs these
itself on first run, so you can skip this line and let it ask for sudo once:

```bash
sudo apt install -y pkg-config libcyaml-dev libevent-dev libjson-c-dev libssl-dev
```

## 2. Clone

```bash
git clone git@github.com:<owner>/<repo>.git /opt/wormhole
cd /opt/wormhole
```

For example, cloning your own fork of this template:

```bash
git clone git@github.com:alice/wormhole.git /opt/wormhole
cd /opt/wormhole
```

`/opt/wormhole` is used throughout this guide; any directory works, as long as the
systemd units in step 7 name the same one.

## 3. Python environment

```bash
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

`requirements-dev.txt` only adds pytest — leave it out on a machine that will never
run the suite, but step 5 is the cheapest verification you get, so install it.

## 4. Configuration

Config is `.env` only. Copy the example and edit it — it documents every key.

```bash
cp .env.example .env
chmod 600 .env          # it will hold the API key and the caster passwords
```

The minimum that must change:

```ini
API_KEY=<long random string>      # X-API-Key for both HTTP APIs
PORT=8000                         # batch file server
STREAM_PORT=9000                  # streaming data plane (must match the device)
STREAM_ADMIN_PORT=9001            # admin API
STREAM_CLI_SECRET=<random token>  # MUST match streaming_cli_secret in the device's CONFIG.TXT
```

Generate the secrets rather than inventing them:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Leave `STREAM_ADMIN_HOST` at its `127.0.0.1` default. The admin API can reboot a
device and push firmware; the shared secret authenticates it but the stream has no
TLS, so the token does not conceal anything. Reach it through an SSH tunnel instead
of widening the bind address:

```bash
ssh -L 9001:localhost:9001 user@<server>
```

## 5. Verify before wiring anything up

Run the test suite — it needs no hardware, no network and no configuration:

```bash
./venv/bin/python -m pytest -q
```

Then a live smoke test. `fake_device.py` connects **as** a station and would evict a
real device holding the same id, which is why it refuses to start without the
opt-in variable:

```bash
./venv/bin/python streaming_server.py &
FAKE_DEVICE_ENABLE=1 ./venv/bin/python fake_device.py \
    --host 127.0.0.1 --port 9000 --station 1001 --role base \
    --secret "$(grep '^STREAM_CLI_SECRET=' .env | cut -d= -f2)" --duration 60 &

curl -s -H "X-API-Key: <key>" http://127.0.0.1:9001/stream/stations
```

Expected: one station, `"connected": true`, frame counters rising, and
**`resync_events` and `garbage_bytes` both 0**. Files appear under
`data/incoming_stream/1001/{ubx,rtcm3,sensors,raw}/`.

The batch server, in a second terminal:

```bash
./venv/bin/python server.py --no-ssl &
curl -s http://127.0.0.1:8000/health
echo test | curl -s -X POST "http://127.0.0.1:8000/modem/upload?device_id=selftest&filename=t.bin" \
     -H "X-API-Key: <key>" -H "Content-Type: application/octet-stream" --data-binary @-
curl -s http://127.0.0.1:8000/uploads -H "X-API-Key: <key>"
```

Stop both again before step 7 puts them under systemd, and delete the test upload
and `data/incoming_stream/1001/`.

## 6. NTRIP caster (optional)

`caster/` bundles a real caster (Millipede), so a fresh install has somewhere to
push to without an account on someone else's caster first. Set the two keys it
reads, in `.env`:

```ini
STREAM_CASTER_PORT=2101
STREAM_CASTER_STATIONS=1001     # one mountpoint per station id, comma-separated
```

Then:

```bash
bash caster/setup.sh
```

The script clones and builds Millipede in place under
`caster/millipede-caster/` (unprivileged, nothing is installed system-wide),
generates `caster.yaml` / `sourcetable.dat` / `source.auth`, and writes a freshly
generated password per station **back into `.env`** as `STREAM_CASTER_PASSWORDS`,
setting `STREAM_CASTER_ENABLE=true`. The caster and the server therefore always
agree on the credential and there is nothing to copy by hand. Re-running it to add
a station never regenerates an existing station's password.

It also writes a **user** systemd unit, `~/.config/systemd/user/millipede-caster.service`:

```bash
systemctl --user enable --now millipede-caster
systemctl --user status millipede-caster
sudo loginctl enable-linger "$USER"   # so it survives logout/reboot without a session
```

Restart the streaming server afterwards — it reads `STREAM_CASTER_ENABLE` at
startup and logs the target on the way up:

```
ntrip caster push: 127.0.0.1:2101, stations [1001]
```

### Verifying the caster

With a station pushing (the real device, or `fake_device.py` from step 5):

```bash
curl -s --max-time 5 http://127.0.0.1:2101/            # sourcetable
curl -s --max-time 10 -H "Ntrip-Version: Ntrip/2.0" http://127.0.0.1:2101/1001 | wc -c
```

The sourcetable must show one `STR;1001;...` line and the pull must return a
growing byte count of RTCM3.

Two behaviours that look like faults and are not:

- **A mountpoint appears in the sourcetable only while a source is actually
  pushing.** With no station connected, `GET /` returns nothing but
  `ENDSOURCETABLE`, and `GET /1001` answers `404` — that is an idle mountpoint, not
  a failed provisioning.
- **Clients need no credentials.** A rover pulls anonymously; the password in
  `source.auth` guards the *push* side only.

And one that is a fault, with a misleading message: `ERROR - Mount Point Taken or
Invalid` on the push side means **field 12 of the `STR;` line is not `0`**. That
field is Millipede's *virtual base* flag, not NTRIP's `nmea` field, and a source may
not push to a virtual mountpoint. The generated sourcetable gets this right; you
only meet it after hand-editing the file or copying a `STR` line out of Millipede's
own sample config. See `caster/README.md`.

## 7. systemd

Two system units, one per server. Adjust `User=` and the paths.

```bash
sudo tee /etc/systemd/system/wormhole.service >/dev/null <<'EOF'
[Unit]
Description=Wormhole IoT File Server (batch)
After=network.target

[Service]
Type=simple
User=wormhole
WorkingDirectory=/opt/wormhole
ExecStart=/opt/wormhole/venv/bin/python server.py --no-ssl
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/wormhole-streaming.service >/dev/null <<'EOF'
[Unit]
Description=Wormhole Streaming Server (TCP data plane + admin API)
# No ordering dependency on the caster: it listens on loopback and the sink
# reconnects on a fixed delay, so a caster that is down must never hold up or
# stop the receiver.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=wormhole
WorkingDirectory=/opt/wormhole
ExecStart=/opt/wormhole/venv/bin/python streaming_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now wormhole wormhole-streaming
systemctl status wormhole-streaming
journalctl -u wormhole-streaming -f
```

Use `Restart=always`, not `Restart=on-failure`. `on-failure` does not cover a clean
exit or an operator stop: one of these receivers was once found dead for nine days
after a manual stop, silently replaced by a hand-started process that did not
survive a reboot. If you ever debug a receiver, check it is the *service* that is
running — `systemctl is-active` — and not a leftover terminal process.

## 8. Firewall

```bash
sudo ufw allow 9000/tcp comment 'wormhole streaming (devices)'
sudo ufw allow 8000/tcp comment 'wormhole batch uploads'
sudo ufw allow 2101/tcp comment 'NTRIP caster (rovers)'
```

Open **9000** for the devices, **8000** if devices upload files, **2101** only if
rovers outside this host pull from the caster. Never open **9001**. `caster/setup.sh`
deliberately touches no firewall rule.

## 9. Point a device at it

In `CONFIG.TXT` on the device's SD card:

```
operation_mode = 1
streaming_server_ip = <server>
streaming_server_port = 9000
streaming_station_id = 1001
streaming_cli_secret = <same as STREAM_CLI_SECRET>
```

`operation_mode = 1` switches batch upload off — the two modes are mutually
exclusive. Confirm the device is really talking to *this* server by watching its
files grow, not by a connection log line alone:

```bash
curl -s -H "X-API-Key: <key>" http://127.0.0.1:9001/stream/stations
watch -n5 'du -sh data/incoming_stream/1001/*'
```

## 10. Several instances on one host

Give each instance its own directory, venv, `.env`, `data/` tree and systemd units,
and one **contiguous port block named after its streaming port** — the batch port
belongs next to the streaming port, not in an unrelated range:

| Instance | Stream | Admin | Caster | Batch |
|---|---|---|---|---|
| `wormhole_9000` | 9000 | 9001 | 9002 | 9003 |
| `wormhole_10000` | 10000 | 10001 | 10002 | 10003 |

The directory name then tells you the whole range at a glance.

One caveat: `caster/setup.sh` always names its user unit `millipede-caster.service`,
so a second instance's setup run overwrites the first instance's unit. Rename the
unit (and its `ExecStart` paths) per instance if you run more than one bundled
caster on one host.

## Updating

```bash
cd /opt/wormhole
git pull
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m pytest -q
sudo systemctl restart wormhole wormhole-streaming
```

Run the suite *before* the restart: it is fast, needs nothing, and catches a
dependency that did not survive the upgrade.

## HTTPS for the batch server

With a domain:

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d <domain>
```

```ini
SSL_CERTFILE=/etc/letsencrypt/live/<domain>/fullchain.pem
SSL_KEYFILE=/etc/letsencrypt/live/<domain>/privkey.pem
```

Then drop `--no-ssl` from the unit's `ExecStart`. Without a domain, `bash
generate-ssl.sh` writes a self-signed pair. The streaming socket has no TLS at all —
see "Not implemented" in `README.md`.

## Disk space

Order of magnitude per station: **~30 MB/h** of `.ubx`, plus RTCM3, plus the raw
capture — which is roughly the sum of both again, since it records every byte a
second time.

The raw capture is pruned at startup (`STREAM_RAW_MAX_AGE_H`, default 7 days). Lower
it for long-term operation; turning it off entirely (`STREAM_RAW_CAPTURE=false`) is
a false economy on a production receiver — see below.

## Operating

```bash
# Who is connected? GNSS clock, frame counters, resyncs, RTCM3 types
curl -s -H "X-API-Key: <key>" http://127.0.0.1:9001/stream/stations

# Send a command to a device (device-side allowlist + shared secret apply)
curl -s -X POST http://127.0.0.1:9001/stream/1001/cli \
     -H "X-API-Key: <key>" -H "Content-Type: application/json" \
     -d '{"cmd": "sysinfo"}'

# Interactive terminal over the same endpoint (reads .env itself)
python3 stream_cli/stream_cli.py --station 1001
```

`resync_events` and `garbage_bytes` staying at **0** is the expected steady state,
not an ideal. A rising count means the stream is being damaged — bytes lost in
transit, or a device tearing its own frames apart. **Do not write it off as
best-effort noise.** That assumption once hid a firmware bug that destroyed 3.5 % of
all RAWX epochs for months: no byte was ever lost, a second producer was writing
into the socket mid-frame, and the framer threw both halves away as garbage. What
identified it was the raw capture — which is why you keep
`STREAM_RAW_CAPTURE=true`. A garbage byte does not mean data was lost; it means the
framer could not place it.
