# Installer (`install.sh`) — 2026-09-23

A menu-driven installer (whiptail, the same dialog tool as `raspi-config`) that
turns an empty Debian/Ubuntu/Raspberry Pi OS machine into a working wormhole
instance, and manages it afterwards. It automates the steps of
`docs/server-deployment.md`. The runtime model does not change: the same
processes, `.env`, `data/` tree and systemd units, written for you.

```bash
curl -fsSLO https://raw.githubusercontent.com/al3xand3w0lf/wormhole/main/install.sh
bash install.sh
```

Run it as a normal user with sudo rights. **Do not pipe it into bash**
(`curl … | bash`): the dialogs need the terminal on stdin, so the script refuses
to run without one. Afterwards `wormhole-setup` (a link in `/usr/local/bin`)
opens the same menu.

## What one install produces

| | |
|---|---|
| Directory | `~/wormhole_<port>` (current user) or `/opt/wormhole_<port>` (system user `wormhole`) — git checkout of the chosen repo/branch |
| Python | `venv/` with `requirements.txt` + `requirements-dev.txt` |
| `.env` | from `.env.example`, `600`, owned by the service user; generated `API_KEY` and `STREAM_CLI_SECRET` (cut to `max_len - 1` of the device field from the config schema, fallback 31: the field size includes the terminator, and a longer secret is silently truncated by the device, which then rejects every CLI command with "auth failed") |
| Units | `wormhole-<port>-stream`, `-batch`, `-caster`, the same names as the manual guide in `server-deployment.md`. They are **system** units with `Restart=always`, enabled at boot |
| Caster | Millipede cloned and built inside the instance by the installer itself (not through `caster/setup.sh`), config from `caster/generate_config.py`. Runs as a system unit under the same user as the streaming server |
| Firewall | ufw rules for the public ports you tick, comment `wormhole_<port> <role>`; the admin port never |
| Registry | `/etc/wormhole/instances/<name>.conf` — ports, components, user, install answers |
| Summary | `<dir>/INSTALL_SUMMARY.txt` (`600`): ports, credentials, the device's `CONFIG.TXT` values, SSH tunnel command |
| Log | `~/wormhole-setup.log` of the user who ran it |

## The dialogs

1. **System check**: OS, systemd, Python ≥ 3.10 (the code uses `X | None` at
   runtime, so 3.9 fails on import), architecture (32-bit ARM: warning), internet,
   NTP sync, free disk, ufw state. A failure stops the install; a warning asks first.
2. **Components**: streaming, NTRIP caster, batch. The caster implies streaming.
3. **Ports**: a block is suggested from the first base port (9000, 10000, …) whose
   four ports are all free and not reserved by another registered instance;
   stream = B, admin = B+1, caster = B+2, batch = B+3. Each port can be changed
   one by one. Continuing validates them: 1024–65535, no duplicates, not
   listening (`ss`), not claimed by another instance.
4. **Service user**: the current user (default) or a dedicated system user
   `wormhole` (created with `useradd --system`, home `/var/lib/wormhole`).
5. **Install directory**.
6. **Public address** (`PUBLIC_HOST`): the address devices dial, prefilled with
   the first LAN address. It is only a suggestion, because behind NAT the right
   answer is the router's public address or a DNS name.
7. **Base station IDs** (optional): written to **both** `STREAM_CASTER_STATIONS`
   (mountpoint now) and `STREAM_ROVER_AUTO_BASE_STATIONS` (trusted as a
   correction source). You can leave it empty. With the caster,
   `STREAM_CASTER_AUTO_ENABLE=true` still gives each base its mountpoint when it
   identifies itself. Rover trust needs the list, and it is read only at
   startup, which is why the dialog asks now instead of later.
8. **Raw capture retention** (`STREAM_RAW_MAX_AGE_H`). The capture itself cannot
   be switched off from here, because it is the forensic safety net.
9. **HTTPS for batch uploads**: none / self-signed (`<dir>/ssl/`, 10 years) /
   Let's Encrypt (certbot standalone, needs port 80; a deploy hook copies the
   renewed certificate where the service user can read it, since
   `/etc/letsencrypt/live/*/privkey.pem` is root-only).
10. **Firewall** (only if ufw is installed): tick the public ports. If ufw is
    inactive, it offers to activate it and allows SSH first.
11. **Confirm**, with an advanced option to change the git source. The default
    source is the checkout the installer runs from (its `origin` and branch).
    A copy downloaded on its own uses the public template. An SSH URL together
    with the dedicated system user is refused here, because that user has no
    SSH key for it.

After the clone, the installer **checks that the code is a version it can set up**
(`install.sh`, the servers, `fake_device.py`, `.env.example`; with the caster
`caster/generate_config.py`; with the caster and no base IDs, a
`generate_config.py` that accepts an empty list). If the code is too old, the
install stops with a message naming what is missing. It does not install half-way.

Each dialog has an answers-file key. The same install then runs without dialogs:

```bash
bash install.sh --unattended answers.env
```

```ini
COMPONENTS=streaming caster batch
BASE_PORT=12000            # or STREAM_PORT / ADMIN_PORT / CASTER_PORT / BATCH_PORT
SERVICE_USER=current       # or system
INSTALL_DIR=/opt/wormhole_12000
PUBLIC_HOST=stations.example.org
BASE_STATIONS=1001,1002
RAW_MAX_AGE_H=168
SSL_MODE=none              # selfsigned | letsencrypt (+ LE_DOMAIN, LE_EMAIL)
FW_OPEN=12000 12002 12003  # ports to open in ufw
FW_ACTIVATE=no
REPO_URL=https://github.com/al3xand3w0lf/wormhole.git
REPO_BRANCH=main
CONTINUE_ON_WARN=yes
RUN_PYTEST=no
```

Exit code 3 means the install finished but the server test failed.

## The server test

It runs at the end of every install, after every settings change and update, and
on demand (`install.sh test <name>`, or "Run the server test" in the menu):

- every unit is active **and** enabled at boot;
- every port is listening, and the **admin port is on 127.0.0.1 only** (anything
  else fails the test, because it would be a security fault);
- admin `/health`, and `/stream/stations` accepts the API key;
- the CLI secret fits the device field (`max_len - 1`);
- `fake_device.py` streams 20 s as station 9999 with **`--role stream`**. With
  `--role base`, caster auto-provisioning would create a mountpoint for the test
  station. The test checks connected, bytes arriving, `garbage_bytes` and
  `resync_events` both 0, then deletes the station's directory. If a real
  station 9999 is connected, the test is skipped rather than evicting it;
- a batch test upload, found in `/uploads`, then deleted;
- the caster answers with a sourcetable (`ENDSOURCETABLE`; empty is normal with no
  base pushing);
- `.env` and `data/` are writable by the service user, because the caster
  auto-provisioning writes `.env` back at runtime;
- optionally the full `pytest` suite.

Reachability from the outside cannot be tested from the machine itself. The
report prints the `nc -vz` line to run from another machine.

## Manage menu

Status (units, since when, restart count, stations, caster mountpoints, disk) ·
server test · generate a device `CONFIG.TXT` (`python -m configgen`, prefilled from
this instance's `.env`) · update · restart · logs (journal, `streaming.log`,
`server.log`, installer log) · settings (ports, `PUBLIC_HOST`, trusted bases, new
CLI secret, raw retention, HTTPS, firewall) · install summary · uninstall.

- **Update** refuses to run if tracked files have local changes. Otherwise it runs
  `git pull --ff-only`, pip, `pytest`, rebuilds and reconfigures the caster only if
  `caster/` changed, and restarts **only** the units whose code changed. A pull changes
  files on disk, not the running process, so the restart cannot be skipped.
- **Settings → Ports** rewrites `.env`, regenerates `caster.yaml` if the caster
  port changed, and replaces the instance's ufw rules. It then reminds you that
  devices need the new streaming port. The instance keeps its name.
- **Settings → trusted bases** only ever *adds* to `STREAM_CASTER_STATIONS`, in
  line with the no-automatic-removal rule of the caster auto-provisioning.
- **Uninstall** removes the units, the firewall rules, the Let's Encrypt hook and
  the registry entry. Deleting the directory with all recorded data takes a
  separate confirmation: you must type the instance name.

CLI equivalents: `install.sh status|test|update|restart|uninstall <name>
[--pytest] [--purge] [--yes]`.

## Resuming

The registry entry is written right after the clone, with `INSTALL_DONE=no`. Every
step checks what already exists: the checkout, the venv, the `.env` (**never
overwritten**), the certificate and the units. Starting the installer again offers
to resume an unfinished instance with its recorded answers. On a failed step the
interactive installer offers retry / show log / roll back (units and firewall
rules; the directory stays) / abort.

## Changes made for it elsewhere

- `caster/setup.sh --no-user-unit`: build and configure only, for manual
  installs. The installer itself no longer calls `setup.sh` (see *The first
  interactive test* below).
- `caster/generate_config.py` accepts an **empty** `STREAM_CASTER_STATIONS` when
  `STREAM_CASTER_AUTO_ENABLE=true`. It writes an empty sourcetable and lets bases
  provision themselves. Before, a customer who did not know their base IDs yet
  got no caster at all. Without auto-provisioning, an empty list is still refused,
  because that caster would stay empty forever
  (`tests/test_caster_generate_config.py`).

## Verified (2026-09-23, on a Raspberry Pi next to a live production instance)

Unattended installs from a snapshot of this branch on port blocks 12000/13000.
The production instance's streaming service kept its start time throughout.

- current user, all components, no base IDs: all 19 checks passed. A fake
  `--role base` station then got its mountpoint auto-provisioned, the SIGHUP
  reached the caster running as a system unit, and an NTRIP pull returned RTCM3.
- `configgen` output: server address, port 12000 and CLI secret match `.env`.
- update with a change under `streaming/`: only the streaming unit restarted, then the
  server test passed.
- install killed during the pip step, started again: resumed, same end state.
- port collision (12000 in use) refused; suggestion skipped 9000 (live instance)
  and offered 10000.
- system user `wormhole` + self-signed HTTPS + base IDs 1901,1902: all checks
  passed; caster ran as `wormhole`; `.env`/summary `600`; sourcetable carried
  both stations. The user was removed afterwards.
- uninstall with and without `--purge`.

**Not verified here:** Let's Encrypt (needs a public DNS name) and a complete
walk-through of the interactive dialogs.

## Verified on a production host with ufw (2026-09-24)

A Debian 13 (trixie), x86_64, Python 3.13 server that already ran five
hand-installed instances (8000-12000) behind an active ufw. The installer came
from GitHub (the customer path), and the instance was a throwaway on port block
13000:

- install in 50 s; all 20 checks passed, including `pytest`;
- ufw: rules for 13000/13002/13003 with the comment `wormhole_13000 <role>`, for
  IPv4 and IPv6. The admin port 13001 got none;
- *Settings → Firewall* path (`st_firewall` with a smaller selection): the
  deselected rule was deleted and the rest kept;
- uninstall `--purge`: all rules gone, and `ufw status` byte-identical to before;
  units, directory, registry entry and `wormhole-setup` link gone;
- the five existing instances were untouched (unit start times compared before
  and after).

**What the installer cannot see:** from outside, 13000-13003 stayed closed while
ufw allowed them. The host sits behind a **provider firewall** (a cloud panel
policy) that opens nothing by default. The same holds for that host's caster
ports 8002-11002. The server test says so ("reachability from outside cannot be
tested from here"), and the summary names the ports. Opening them at the
provider is a step outside the machine.

## The first interactive test (2026-09-24), and what it changed

The installer was started from a private fork's checkout, but the code source was
left at its default at the time: the **public template**. That template was on
the 2026-09-06 state, without `install.sh` or `configgen/`. Two things followed:

- **The instance had no `install.sh`.** `wormhole-setup` still worked, but only
  as a loose copy in `/usr/local/bin`. The expectation, reasonably, was
  `./install.sh` in the instance directory.
- **The template's old `caster/setup.sh` overwrote a production unit.** It did not
  know `--no-user-unit`, ignored it, and wrote its fixed user unit
  `~/.config/systemd/user/millipede-caster.service`. That unit was running the
  host's production caster. The running process was unaffected; the
  next restart or reboot would have started the test instance's caster instead.
  The unit had to be restored by hand.

Three changes answer that:

1. The installer **builds Millipede itself** (clone, `make`, `generate_config.py`)
   instead of calling `caster/setup.sh`. Whatever that script does in any version,
   the installer never runs it.
2. The **default code source is the checkout the installer runs from**. The public
   template is only the default for a downloaded copy.
3. **The code is checked after the clone** (see *The dialogs*). A too-old template
   now fails at step 3 with a message, before anything is built or registered.
   `wormhole-setup` is always a link to the instance's own `install.sh`.

Verified the same day: the public template is refused at the code step; a full
install with the detected source (the fork's `origin`, `main`) passes all
19 checks; the host's `millipede-caster.service` user unit is byte-identical
before and after (md5).

**Before customers can use it:** the public `wormhole` template must carry the
current `main`. Until then, a downloaded installer refuses the template at the
code step. That is the intended failure, but it means the `curl` one-liner does
not install anything yet.
