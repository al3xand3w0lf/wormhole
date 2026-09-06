# Bundled NTRIP caster

A working NTRIP caster, wired to this repo's own streaming server, so a rover
that cannot use — or simply doesn't want — the direct TCP stream (see the
"Streaming Server" section of the root `README.md`) can instead pull its
corrections as a standard NTRIP client. The streaming server can already push
RTCM3 to *any* NTRIP caster (`streaming/ntrip.py`, `STREAM_CASTER_*` in
`.env.example`); this directory is what makes that useful out of the box,
without requiring an account on someone else's caster first.

Software: [Millipede](https://github.com/pbeyssac/millipede-caster) (C,
libevent2, built for the Centipede-RTK project). Chosen over the other
NTRIP-caster options mainly for one reason: it authenticates sources with a
**password per mountpoint**, matching this repo's one-mountpoint-per-station
model — some other casters (e.g. BKG's reference implementation) use a single
global source password for every mountpoint, which is a weaker default when
several stations share one caster.

## What "bundled" means here

- One extra local process (`caster/millipede-caster/caster/caster`), started
  and supervised by a **user-level** systemd unit — no root needed for
  day-to-day operation, same pattern as this repo's other services.
- Built from source in place under `caster/millipede-caster/`, never
  `make install`ed to `/usr/local` — nothing outside this directory changes
  on your system, and removing the caster is `rm -rf caster/millipede-caster`.
- One mountpoint per station, name == station id, auto-generated from
  `STREAM_CASTER_STATIONS` in `.env` — no manual per-mountpoint setup.
- The streaming server pushes into it over loopback
  (`STREAM_CASTER_HOST=127.0.0.1`); the caster itself listens on
  `0.0.0.0:STREAM_CASTER_PORT` so *rovers* can reach it from elsewhere.
  Loopback push + externally-reachable listen are independent — the caster
  binding to all interfaces does not make the streaming server's own ports
  (`:9000`/`:9001`) any more exposed than they already are.

## Setup

```bash
cp ../.env.example ../.env   # if you haven't already
# edit .env: set STREAM_CASTER_STATIONS to the station id(s) you want a
# mountpoint for, e.g. STREAM_CASTER_STATIONS=1001
bash setup.sh
```

`setup.sh`:
1. Installs build dependencies via `apt` (`pkg-config libcyaml-dev
   libevent-dev libjson-c-dev libssl-dev git`) — the **only** step needing
   `sudo`, and only the first time.
2. Clones and builds Millipede in place under `caster/millipede-caster/`.
3. Runs `generate_config.py`, which:
   - writes `caster/millipede-caster/etc/{caster.yaml,sourcetable.dat,source.auth}`
     from `STREAM_CASTER_STATIONS` / `STREAM_CASTER_PORT`;
   - generates a fresh random password for any station that doesn't have one
     yet, and writes it into `.env` as `STREAM_CASTER_PASSWORDS` (also sets
     `STREAM_CASTER_ENABLE=true`) — so the caster and the streaming server's
     `NtripCasterSink` always agree on the credential, with nothing to copy
     by hand.
   - is **idempotent**: re-running it (e.g. after adding a station to
     `STREAM_CASTER_STATIONS`) never changes a password that already exists.
4. Installs a **user-level** systemd unit (`~/.config/systemd/user/millipede-caster.service`).

Two things `setup.sh` deliberately leaves to you:

- **`sudo loginctl enable-linger $USER`** — one-time, needs sudo, only
  required if you want the caster to keep running after you log out /
  across a reboot without an active session. Skip it if you're fine
  starting it manually each time, or if it's already run as a system
  service elsewhere.
- **Firewall.** The caster listens on all interfaces so external rovers can
  reach it, but this repo does not touch your firewall for you. Open
  `STREAM_CASTER_PORT/tcp` however you manage that (e.g.
  `sudo ufw allow 2101/tcp comment 'NTRIP caster (wormhole)'`).

Then:

```bash
systemctl --user enable --now millipede-caster
systemctl --user status millipede-caster
```

Restart the streaming server — `STREAM_CASTER_ENABLE=true` is now in `.env`,
so `_make_sinks()` attaches an `NtripCasterSink` for every station listed in
`STREAM_CASTER_PASSWORDS`.

## Verifying it

```bash
# Sourcetable is being served:
curl http://127.0.0.1:2101/

# Once the streaming server has pushed at least one RTCM3 frame for the
# station, the mountpoint shows up as a live source (Millipede only lists a
# local mountpoint in GET / while something is actually connected to it —
# an "empty" sourcetable does not mean the config failed to load):
curl http://127.0.0.1:2101/1001
```

`caster/millipede-caster/var/log/caster.log` is the caster's own log —
`Reloading …sourcetable.dat` on startup confirms the generated config
parsed.

## Adding a station later

With `STREAM_CASTER_AUTO_ENABLE=true` in `.env` there is nothing to do: a
station that identifies with `role=base` provisions itself the moment it
connects — password generated, both files below rewritten, caster SIGHUPed, push
started on the live session, and `STREAM_CASTER_STATIONS` /
`STREAM_CASTER_PASSWORDS` written back into `.env`. See `GET /stream/caster` on
the admin port to see what it did.

By hand, or for a station that is not a base: edit `STREAM_CASTER_STATIONS` in
`.env`, then

```bash
python3 generate_config.py   # regenerates the sourcetable + adds a password
                              # for the new station only
sudo systemctl reload millipede-caster   # or: systemctl --user reload ...
```

Both paths render `sourcetable.dat` / `source.auth` through the same code
(`streaming/caster_config.py`) and keep each other's stations and passwords, so
running the script after an auto-provisioning is safe and changes nothing.

A `SIGHUP`/reload does **not** drop already-connected sources — you can add a
mountpoint without interrupting a station that's already pushing.

## The "Mount Point Taken" trap (already avoided here, worth knowing about)

Millipede reads field 12 of a sourcetable `STR;` line — NTRIP's *nmea* field
— as its own **"virtual base"** flag. A `SOURCE` push to a mountpoint flagged
virtual is refused with the misleading error text `ERROR - Mount Point Taken
or Invalid`, which reads like the mountpoint is already in use when the real
problem is that field. Millipede's own sample sourcetable's only `STR` lines
are virtual bases carrying `1` there, so copying them as a template produces
an unpushable mountpoint. `generate_config.py` always writes `0` — if you
ever hand-edit `sourcetable.dat`, keep it that way.

## Nearest-base routing ("NEAR")

Millipede can route a client to whichever real mountpoint is geographically
closest to it, automatically — a rover just connects to a fixed virtual
mountpoint named `NEAR` and sends its position as NMEA GGA on the same socket;
Millipede computes the distance to every real `STR` entry in the sourcetable
and proxies through the nearest one, re-checking as the client moves
(`caster/millipede-caster/caster/ntripsrv.c`, `ntripsrv_redo_virtual_pos()`).
Nothing in this repo implements the selection itself — it is entirely
Millipede's own sourcetable-driven feature; see its own
[`README.md`](millipede-caster/README.md#near-base) for the mechanism.

`generate_config.py` sets this up for you:

- It decodes each station's real position from its own RTCM 1005/1006 ARP,
  read from the most recent file in its `.rtcm3` archive
  (`STREAM_DIR/<id>/rtcm3/`), and writes it into that station's `STR` line —
  the real position is what NEAR actually compares against, so a mountpoint
  stuck at the `0.00/0.00` placeholder can never be correctly selected as
  nearest. A station with no archive yet (never pushed a frame) keeps the
  placeholder until you re-run `generate_config.py` after it has.
- A `STR;NEAR;...` line (the "virtual" field set, per Millipede's own
  convention) is added automatically once at least one station has a real
  position — no separate flag to turn on.

**With only one real base this is a harmless no-op** — there is nothing to be
"nearer" than — and starts actually selecting the moment a second station gets
its own decoded position. See `docs/millipede-near-base-2026-08-28.md` for the
current state of this deployment and the rover-side `config.txt` block
(`ntrip_mountpoint = NEAR`, `ntrip_send_gga = 1`).

## Not set up here

TLS, the `proxy` feature (fetching a mountpoint from another caster), Graylog/
GELF export, and the admin JSON API (`admin_user` needs a password mechanism
this setup doesn't configure) are all things Millipede supports but this
bundled setup doesn't turn on. `caster/millipede-caster/etc/caster.yaml` is a
real config file — edit it directly for any of that (re-running
`generate_config.py` will overwrite the parts it manages: `listen`, the
`*_file` paths, and the log section, so keep any manual additions in the parts
it leaves alone, or copy them back in after re-running).
