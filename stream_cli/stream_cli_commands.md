# stream_cli.py — command reference

An interactive terminal for the streaming server's remote CLI. It talks to the
server's admin API (`:9001`); the server sends the command to the device as frames
and reassembles the reply. The script itself contains **no** frame logic and never
talks to a device directly.

On the server host it needs **no configuration**: `API_KEY` and the admin port are
read straight from the repo `.env`.

---

## 0. Preparation (activate the venv)

```bash
cd <install dir>            # the repo root
source venv/bin/activate
```

`python` is then on the path. To check:

```bash
which python                # -> <install dir>/venv/bin/python
```

Leave the venv later with `deactivate`.

> **Paths:** the examples below run **from the repo root** and therefore say
> `stream_cli/stream_cli.py`. From inside `stream_cli/`, drop the prefix:
> `python stream_cli.py --list`. (The `.env` is located relative to the script,
> so the working directory does not matter.)

> **`python` vs. `python3`:** inside an activated venv, `python` works. Without
> one, many systems only have `python3`. The examples assume the activated venv.

---

## 1. Which stations are online?

```bash
python stream_cli/stream_cli.py --list
```

Example output:

```
 station  connected  gps_time                     pending  peer
    1001       True  2026-07-15T19:50:57            False  203.0.113.5:13930
```

| Column | Meaning |
|---|---|
| `station` | station id |
| `connected` | `True` = the TCP stream is currently connected |
| `gps_time` | the station's current GPS clock (empty before its first fix) |
| `pending` | `True` = a CLI request is in flight right now |
| `peer` | IP:port of the device connection |

---

## 2. Connect to a station (open the REPL)

```bash
python stream_cli/stream_cli.py --station 1001
```

If exactly **one** station is connected, `--station` can be omitted and that one is
picked automatically:

```bash
python stream_cli/stream_cli.py
```

You then get a prompt:

```
Connected to http://127.0.0.1:9001, station 1001. Type /help, /quit to exit.
1001>
```

---

## 3. Talk to the station

In the REPL, type the device command straight after the prompt:

```
1001> sysinfo
1001> whoami
1001> listfiles
```

What comes back is whatever the device sends. Example — a firmware that reports its
RTOS task list for `sysinfo`:

```
1001> sysinfo
Task name                Run time  Free stack
---------------------------------------------
StreamTask        25981271     1%     11200 B
...
Free heap              18032 B
---------------------------------------------
```

### Allowed device commands (allowlist)

The device accepts only these (a positive allowlist — everything else comes back as
`command not permitted`):

| Command | Purpose |
|---|---|
| `whoami` | identify the station |
| `sysinfo` | firmware / system info (tasks, heap) |
| `listfiles` | list the files on the device's storage |
| `download <file>` | fetch a file **from the server onto the device** (special case, see below) |
| `downloadcf` | fetch the config file from the server (no reboot — `reboot` is what applies it) |
| `downloadfw` | fetch a firmware image from the server (no reboot — `reboot` is what flashes it) |
| `upload <file>` | push a file **from the device to the server** (logs, config) |
| `reboot` | restart the device |
| `fsdcard` | ⚠️ **formats the storage card immediately** — no confirmation prompt, not a status query. Anything not yet uploaded is gone. For usage, ask `listfiles`. |

A firmware that echoes the verb it received reports a rejected command as
`command not permitted: donwloadcf`, which is what makes a typo distinguishable from
a deliberately blocked command. **The allowlist is enforced on the device**; the copy
in `stream_cli.py` only feeds `/help`.

### REPL meta-commands (leading `/`)

| Meta-command | Effect |
|---|---|
| `/stations` | re-list the stations |
| `/station <id>` | switch the active station |
| `/help` | show the allowlist + meta-commands |
| `/quit`, `/exit`, `/q` | leave the REPL (Ctrl-D works too) |

### Special case: `download` / `downloadfw`

These pause the stream, the device transfers the file over its own modem,
reconnects, and **only then** sends the real answer. The script prints a note and
waits accordingly (client timeout 620 s, matched to the server's 600 s transfer
timeout). Needs a file that actually exists on the device.

```
1001> download measurement.bin
  (download-class command — device pauses, transfers, reconnects; this can take a while)
download measurement.bin: transfer complete
```

Note that a device with file transfer over the stream disabled falls back to the
older dance (ack → disconnect → reconnect → deferred answer). The server serves both
and the client sees no difference beyond the wait.

---

## One-shot mode (no REPL)

Send one command, print the answer, exit — for scripts and CI:

```bash
python stream_cli/stream_cli.py --station 1001 sysinfo
python stream_cli/stream_cli.py --station 1001 whoami
python stream_cli/stream_cli.py sysinfo            # station picked automatically if only one is online
```

---

## All options

| Option | Meaning |
|---|---|
| `cmd ...` | one-shot command (omit for the interactive REPL) |
| `--station <id>` | station id (omitted picks the sole connected one) |
| `--list` | list stations and exit |
| `--url <url>` | override the admin base URL (default: from `.env`) |
| `--api-key <key>` | override the X-API-Key (default: from `.env`) |
| `--timeout <s>` | server-side wait for the response (default: the server's own) |
| `-h`, `--help` | show help |

### Config resolution (first hit wins)

The same order applies to the API key and the admin URL:

1. `--api-key` / `--url` on the command line
2. `$API_KEY` / `$STREAM_ADMIN_URL` in the environment
3. the repo `.env` (`API_KEY`, `STREAM_ADMIN_PORT`) — the zero-setup path on the host
4. the `CONFIG` defaults inside the script (for copying it onto a machine with no `.env`)

---

## From another machine (SSH tunnel)

The admin port `9001` is never exposed publicly — it binds `STREAM_ADMIN_HOST`
(default `127.0.0.1`). Reach it from elsewhere through a tunnel:

```bash
ssh -L 9001:127.0.0.1:9001 <user>@<host>     # leave this open in one terminal
python stream_cli/stream_cli.py --url http://127.0.0.1:9001 --api-key <KEY> --station 1001
```

The shared secret matters as much as the tunnel: the CLI is authenticated by
`STREAM_CLI_SECRET`, which must match the device's own configured secret. The stream
has no TLS, so that token authenticates — it does not conceal.
