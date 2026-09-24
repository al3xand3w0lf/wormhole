# Wormhole Groundstation — Setup Guide

Builds a complete groundstation instance from an empty Debian/Ubuntu machine: the
batch file server, the streaming receiver, and the NTRIP caster bundled in
`caster/`. The same steps set up a second, third, … instance on a host that already
runs one — every name and port below is derived from one number, so instances never
collide.

Follow it top to bottom. Every step ends with a check; do not go on while a check
fails — the mistake is cheapest to fix where it was made.

## Quick install (recommended)

`install.sh` walks through everything below in a menu and produces the same
instance: directory `/opt/wormhole_N` (or `~/wormhole_N`), units
`wormhole-N-{stream,batch,caster}`, the four ports N…N+3, `.env` with generated
secrets, firewall rules and a server test at the end. It also manages the instance
afterwards: status, update, settings, device `CONFIG.TXT`, uninstall.

🖥 **server** — as a normal user with sudo rights (or root):

```bash
curl -fsSLO https://raw.githubusercontent.com/al3xand3w0lf/wormhole/main/install.sh
bash install.sh
```

Download it first and then run it, as above. `curl … | bash` does not work,
because the menu needs the terminal. Later, `wormhole-setup` opens the same menu.
Details: `docs/installer-2026-09-23.md`.

The manual steps below are the reference for what the installer does, and the way
to go where it does not fit (another OS, a hand-tuned setup).

## Before you start: three rules for reading this guide

**1. Every code block says where it runs.**

| Marker | Where |
|---|---|
| 🖥 **server** | In an SSH session **on the server** (as root, or with `sudo`) |
| 💻 **your PC** | In a terminal **on your own computer**, *not* inside the SSH session |

Running a 💻 command on the server does not fail loudly — it just does something
useless, and can even block a port the server needs (see *Troubleshooting*).

**2. There are no `<placeholders>` to type over.** Every block uses the shell
variables you set once in step 0. If you open a new SSH session, **run the step-0
block again first** — shell variables do not survive a logout.

If you ever copy a command that still contains `<something>` literally (from an
older guide, a chat, a README), the shell stops with
`syntax error near unexpected token 'newline'` — `<` and `>` are redirections to
bash. Replace the whole `<…>`, brackets included.

**3. Never copy `.env` from another instance.** It carries that instance's secrets
and ports. Start from `.env.example` (step 4).

## What you end up with

Three independent processes per instance. They share the instance directory, the
venv, the `.env` and the `data/` tree, but run as separate systemd services — one
crashing or being restarted does not touch the others.

| Process | Port | Reachable from | Role |
|---|---|---|---|
| `streaming_server.py` | **N** | Internet (devices) | Streaming mode: live byte stream, one socket per station |
| ↳ its admin API | **N+1** | **loopback only** | Status + remote CLI — reached through an SSH tunnel, never opened |
| Millipede caster | **N+2** | Internet (rovers), optional | NTRIP caster: rovers pull the corrections a base pushed in |
| `server.py` | **N+3** | Internet (devices) | Batch mode: devices upload finished files |

**N** is the instance number and its streaming port, e.g. `11000`. The directory,
the service names and all four ports follow from it:

| Instance | Directory | Services | Stream | Admin | Caster | Batch |
|---|---|---|---|---|---|---|
| 9000 | `/opt/wormhole_9000` | `wormhole-9000-{stream,batch,caster}` | 9000 | 9001 | 9002 | 9003 |
| 10000 | `/opt/wormhole_10000` | `wormhole-10000-…` | 10000 | 10001 | 10002 | 10003 |
| 11000 | `/opt/wormhole_11000` | `wormhole-11000-…` | 11000 | 11001 | 11002 | 11003 |

Pick an N whose four ports are all free (step 0 checks that). Use steps of 1000.

---

## 0. Set the instance variables

🖥 **server** — adjust the first two lines, paste the whole block:

```bash
N=11000                              # instance number = streaming port
SERVER_IP=203.0.113.10            # this server's public address (for the device config)

DIR=/opt/wormhole_$N
ADMIN=$((N+1)); CASTER=$((N+2)); BATCH=$((N+3))
echo "instance $N in $DIR — stream $N, admin $ADMIN, caster $CASTER, batch $BATCH"
```

✅ **Check** — the ports must be free and the directory must not exist yet:

```bash
ss -ltn | grep -E ":($N|$ADMIN|$CASTER|$BATCH) " && echo "PORT IN USE - pick another N" || echo "ports free"
test -e "$DIR" && echo "$DIR EXISTS - pick another N" || echo "directory free"
```

Both lines must say *free*.

## 1. Packages

🖥 **server** (once per host — skip on a host that already runs an instance):

```bash
apt update
apt install -y python3 python3-venv python3-pip git curl \
               pkg-config libcyaml-dev libevent-dev libjson-c-dev libssl-dev
```

The second line is only for the bundled caster (step 6).

## 2. Clone

🖥 **server** — over HTTPS, so no GitHub key is needed on the server:

```bash
git clone https://github.com/al3xand3w0lf/wormhole.git "$DIR"
cd "$DIR"
```

Clone it — do not download and unpack a ZIP. A ZIP has no `.git`, so the
*Updating* section (`git pull`) cannot work, and it loses the executable bits of the
scripts.

✅ **Check:** `git -C "$DIR" log --oneline -1` prints a commit.

## 3. Python environment

🖥 **server**

```bash
cd "$DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
./venv/bin/python -m pytest -q
```

The test suite needs no hardware, no network and no configuration.

✅ **Check:** the last line says `… passed` and no `failed`.

## 4. Configuration (`.env`)

🖥 **server** — creates `.env`, sets the ports from N and **generates and writes
both secrets** in one go. Nothing to type by hand:

```bash
cd "$DIR"
cp .env.example .env
chmod 600 .env

# set KEY=VALUE, whether the key is present, commented out (# KEY=) or missing
setenv() { if grep -qE "^#? ?$1=" .env; then sed -i -E "s|^#? ?$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
gen()    { python3 -c "import secrets; print(secrets.token_urlsafe(24))"; }

setenv API_KEY            "$(gen)"
setenv STREAM_CLI_SECRET  "$(gen)"
setenv PORT               $BATCH
setenv STREAM_PORT        $N
setenv STREAM_ADMIN_HOST  127.0.0.1
setenv STREAM_ADMIN_PORT  $ADMIN
setenv STREAM_CASTER_PORT $CASTER
setenv STREAM_CASTER_STATIONS 1001        # the station id(s) of this instance, comma-separated
```

✅ **Check** — prints the active settings with the secrets masked:

```bash
grep -vE '^\s*(#|$)' .env | sed -E 's/^((API_KEY|STREAM_CLI_SECRET|STREAM_CASTER_PASSWORDS)=).+/\1***/'
```

You must see `API_KEY=***`, `STREAM_CLI_SECRET=***` and the four ports of this
instance. `API_KEY` must **not** be `changeme`.

What the two secrets are for:

| Key | Used by | Must match |
|---|---|---|
| `API_KEY` | `X-API-Key` header of both HTTP APIs (batch + admin) | Your scripts / curl calls; the device's batch-upload key if it uploads in batch mode |
| `STREAM_CLI_SECRET` | Authenticates remote CLI commands to a device | `streaming_cli_secret` in the device's `CONFIG.TXT` (step 9) |

You read them back later with `grep '^API_KEY=' "$DIR/.env"` — no need to write
them down anywhere else.

The admin API (port N+1) stays on `127.0.0.1`. It can reboot a device and push
firmware; do not widen the bind address. You reach it through an SSH tunnel from
your PC (step 10).

## 5. Smoke test by hand

🖥 **server** — start the streaming server in the foreground of this terminal:

```bash
cd "$DIR"
./venv/bin/python streaming_server.py
```

🖥 **server, a second SSH session** (re-run step 0 there first!) — a fake station
connects for 60 s:

```bash
cd "$DIR"
FAKE_DEVICE_ENABLE=1 ./venv/bin/python fake_device.py \
    --host 127.0.0.1 --port $N --station 1001 --role base \
    --secret "$(grep '^STREAM_CLI_SECRET=' .env | cut -d= -f2)" --duration 60 &
sleep 15
curl -s -H "X-API-Key: $(grep '^API_KEY=' .env | cut -d= -f2)" http://127.0.0.1:$ADMIN/stream/stations
```

✅ **Check:** one station, `"connected": true`, frame counters above 0, and
**`resync_events` and `garbage_bytes` both 0**.

`fake_device.py` connects **as** station 1001 and would evict a real device holding
that id — which is why it refuses to start without `FAKE_DEVICE_ENABLE=1`. Only run
it before a real device points at this instance.

Stop the server in the first terminal (Ctrl+C) and clean up:

```bash
rm -rf "$DIR/data/incoming_stream/1001"
```

## 6. NTRIP caster (optional)

Skip this step if no rover will pull corrections from this instance — and then also
skip the caster unit in step 7 and its firewall line in step 8.

🖥 **server** — build Millipede inside the instance and generate its config:

```bash
cd "$DIR/caster"
git clone https://github.com/pbeyssac/millipede-caster.git millipede-caster
make -C millipede-caster/caster
"$DIR/venv/bin/python3" generate_config.py
```

`generate_config.py` writes `caster.yaml`, `sourcetable.dat` and `source.auth`
under `caster/millipede-caster/etc/`, generates one push password per station and
writes it **back into `.env`** as `STREAM_CASTER_PASSWORDS`, together with
`STREAM_CASTER_ENABLE=true`. Caster and streaming server therefore always agree on
the credential. Re-running it (e.g. after adding a station to
`STREAM_CASTER_STATIONS`) never changes an existing station's password.

✅ **Check:**

```bash
ls -l "$DIR/caster/millipede-caster/caster/caster"
grep -E '^STREAM_CASTER_(ENABLE|STATIONS|PORT)=' "$DIR/.env"
```

> Plain `caster/setup.sh` does the same build, but also installs a *user* unit
> that is always called `millipede-caster.service` — on a server with several
> instances a second one silently overwrites the first one's. Step 7 uses a
> per-instance system unit instead; `bash caster/setup.sh --no-user-unit` is the
> build + config above without that user unit.

The caster's own behaviour (idle mountpoints answer 404, anonymous pull, the
"Mount Point Taken" trap) is described under *Verifying the caster* below and in
`caster/README.md`.

## 7. systemd services

🖥 **server** — writes up to three units named after the instance. Paste the whole
block; the variables from step 0 are filled in:

```bash
cat > /etc/systemd/system/wormhole-$N-stream.service <<EOF
[Unit]
Description=Wormhole streaming server ($N group, TCP data port $N, admin port $ADMIN)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$DIR
Environment=STREAM_LOG_FILE=$DIR/streaming.log
ExecStart=$DIR/venv/bin/python3 streaming_server.py --port $N --admin-port $ADMIN
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/wormhole-$N-batch.service <<EOF
[Unit]
Description=Wormhole IoT File Server ($N group, batch upload, port $BATCH)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$DIR
Environment=LOG_FILE=$DIR/server-batch.log
ExecStart=$DIR/venv/bin/python3 server.py --no-ssl --port $BATCH
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# only if you did step 6:
cat > /etc/systemd/system/wormhole-$N-caster.service <<EOF
[Unit]
Description=Millipede NTRIP caster ($N group, port $CASTER)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$DIR/caster/millipede-caster
ExecStart=$DIR/caster/millipede-caster/caster/caster -c $DIR/caster/millipede-caster/etc/caster.yaml
ExecReload=/bin/kill -HUP \$MAINPID
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now wormhole-$N-caster wormhole-$N-stream wormhole-$N-batch
```

(Without step 6, leave `wormhole-$N-caster` out of the last line.)

The same two server units are in the repo as `wormhole-streaming.service` and
`wormhole.service` (with `@N@`-style placeholders and a `sed` line in their header),
for when you want to install them without this block.

✅ **Check:**

```bash
systemctl is-active wormhole-$N-stream wormhole-$N-batch wormhole-$N-caster
ss -ltnp | grep -E ":($N|$ADMIN|$CASTER|$BATCH) "
journalctl -u wormhole-$N-stream -n 20 --no-pager
```

All `active`; the four ports listening — N, N+2 and N+3 on `0.0.0.0`, N+1 on
`127.0.0.1` only. With the caster enabled the stream log shows
`ntrip caster push: 127.0.0.1:<N+2>, stations [1001]`.

Notes:

- `Restart=always`, not `on-failure`: `on-failure` does not cover a clean exit or an
  operator stop. One receiver was once found dead for nine days after a manual stop,
  silently replaced by a hand-started process that did not survive a reboot. When
  debugging, check it is the *service* that runs (`systemctl is-active`), not a
  leftover terminal process.
- The ports are passed on the command line **and** set in `.env`. The command line
  wins; `.env` is what the tools (`stream_cli`, `generate_config.py`) read. Keep both
  equal — step 0/4 does that for you.
- `User=root` is what the existing instances use. For a dedicated user instead:
  `useradd -r -s /usr/sbin/nologin wormhole && chown -R wormhole: "$DIR"`, then
  `User=wormhole` in all three units.

## 8. Firewall

🖥 **server** (ufw):

```bash
ufw allow $N/tcp      comment "wormhole $N stream (devices)"
ufw allow $BATCH/tcp  comment "wormhole $N batch (devices)"
ufw allow $CASTER/tcp comment "wormhole $N caster (rovers)"   # only with step 6
ufw status | grep -E "^($N|$CASTER|$BATCH)/"
```

**Never open N+1** (admin).

If the server sits behind a provider firewall (IONOS/1&1 cloud panel, Hetzner,
AWS security group, …) open the same ports there too — ufw alone is then not enough.

✅ **Check** — 💻 **your PC**, fill in the address and port by hand this once:

```bash
nc -vz 203.0.113.10 11000      # must say "succeeded"/"open"
```

## 9. Point a device at it

Print the exact lines for the device — 🖥 **server**:

```bash
echo "operation_mode = 1
streaming_server_ip = $SERVER_IP
streaming_server_port = $N
streaming_station_id = 1001
streaming_cli_secret = $(grep '^STREAM_CLI_SECRET=' "$DIR/.env" | cut -d= -f2)"
```

Copy them into `CONFIG.TXT` on the device's SD card. `operation_mode = 1` switches
batch upload off — the two modes are mutually exclusive.

Confirm the device is really talking to *this* instance by watching its files grow,
not by a connection log line alone — 🖥 **server**:

```bash
curl -s -H "X-API-Key: $(grep '^API_KEY=' "$DIR/.env" | cut -d= -f2)" http://127.0.0.1:$ADMIN/stream/stations
watch -n5 "du -sh $DIR/data/incoming_stream/*/*"
```

## 10. Reach the admin API from your PC (SSH tunnel)

💻 **your PC** — *not* in the SSH session on the server. Example for instance 11000:

```bash
ssh -L 11001:localhost:11001 root@203.0.113.10
```

Keep that window open. While it is, `http://localhost:11001/…` **on your PC** is the
admin API of that instance, e.g. (💻 your PC, second terminal):

```bash
curl -s -H "X-API-Key: PASTE_API_KEY" http://localhost:11001/stream/stations
```

Close the tunnel with `exit`. Typed on the server, the same `ssh -L` command opens
an SSH session from the server to itself and **grabs port N+1 on the server** — the
streaming service can then no longer bind its admin port (see *Troubleshooting*).

---

## Verifying the caster

🖥 **server**, with a station pushing (the real device, or `fake_device.py` from
step 5):

```bash
curl -s --max-time 5 http://127.0.0.1:$CASTER/            # sourcetable
curl -s --max-time 10 -H "Ntrip-Version: Ntrip/2.0" http://127.0.0.1:$CASTER/1001 | wc -c
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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `syntax error near unexpected token 'newline'` | A literal `<placeholder>` was typed | Replace the whole `<…>` including the brackets |
| `address already in use` in the stream log, admin API not answering | Something else holds port N+1 — typically an `ssh -L` that was run **on the server** | `ss -ltnp \| grep :$ADMIN` shows the owner; close that SSH session (`exit`) or `kill` the `ssh -L` pid, then `systemctl restart wormhole-$N-stream` |
| `curl` to the admin API: `401 Invalid API key` | Wrong or missing `X-API-Key` | Use the key from **this** instance's `.env` |
| Device connects but remote CLI is refused | `streaming_cli_secret` on the device ≠ `STREAM_CLI_SECRET` | Re-print step 9 and fix `CONFIG.TXT` |
| Stream log: `STREAM_CLI_SECRET is empty` | Step 4 skipped or incomplete | Run step 4's `setenv STREAM_CLI_SECRET "$(gen)"`, restart the stream service |
| Stream log keeps reconnecting to the caster | `STREAM_CASTER_ENABLE=true` but no caster running (step 6 skipped, or `.env` copied from another instance) | Do step 6 + the caster unit, or set `STREAM_CASTER_ENABLE=false` |
| `git pull`: `not a git repository` | Code was unpacked from a ZIP | `git init -b main && git remote add origin https://github.com/al3xand3w0lf/wormhole.git && git fetch origin main && git reset origin/main && git checkout -- .` (keeps `.env`, `venv/`, `data/` — they are git-ignored) |
| Device cannot connect at all, `nc` from your PC fails | Port not open in ufw **or** in the provider's firewall | Step 8 |

Who holds which port, at any time: `ss -ltnp | grep -E ":($N|$ADMIN|$CASTER|$BATCH) "`.

## Updating

🖥 **server** (step 0 first):

```bash
cd "$DIR"
git pull
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
./venv/bin/python -m pytest -q
systemctl restart wormhole-$N-stream wormhole-$N-batch
```

Run the suite *before* the restart: it is fast, needs nothing, and catches a
dependency that did not survive the upgrade.

## HTTPS for the batch server

With a domain:

```bash
apt install -y certbot
certbot certonly --standalone -d your.domain.example
```

```ini
SSL_CERTFILE=/etc/letsencrypt/live/your.domain.example/fullchain.pem
SSL_KEYFILE=/etc/letsencrypt/live/your.domain.example/privkey.pem
```

Then drop `--no-ssl` from the batch unit's `ExecStart`. Without a domain,
`bash generate-ssl.sh` writes a self-signed pair. The streaming socket has no TLS at
all — see "Not implemented" in `README.md`.

## Disk space

Order of magnitude per station: **~30 MB/h** of `.ubx`, plus RTCM3, plus the raw
capture — which is roughly the sum of both again, since it records every byte a
second time.

The raw capture is pruned at startup (`STREAM_RAW_MAX_AGE_H`, default 7 days). Lower
it for long-term operation; turning it off entirely (`STREAM_RAW_CAPTURE=false`) is
a false economy on a production receiver — see below.

## Operating

🖥 **server** (step 0 first):

```bash
KEY=$(grep '^API_KEY=' "$DIR/.env" | cut -d= -f2)

# Who is connected? GNSS clock, frame counters, resyncs, RTCM3 types
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:$ADMIN/stream/stations

# Send a command to a device (device-side allowlist + shared secret apply)
curl -s -X POST http://127.0.0.1:$ADMIN/stream/1001/cli \
     -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"cmd": "sysinfo"}'

# Interactive terminal over the same endpoint (reads .env itself)
cd "$DIR" && ./venv/bin/python stream_cli/stream_cli.py --station 1001
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
