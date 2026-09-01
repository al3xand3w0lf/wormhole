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

## Menu instead of typing: `stream_menu.py`

If you would rather not type the commands, start the menu:

```bash
python stream_cli/stream_menu.py
```

It asks for the station first (only **connected** ones can be picked), then for the
command:

```
  Stations on http://127.0.0.1:9001

    1) 1001   gps 2026-08-31T18:10:00   203.0.113.5:8910
    2) 2201   gps 2026-08-31T18:10:00   203.0.113.47:61529

    a) also show the 3 station(s) that are known but not connected
    r) reload
    q) quit

  Station> 1

  Station 1001 — commands

    1) whoami             identify the station
    2) sysinfo            firmware / system info (tasks, heap)
    3) listfiles          list the files on the device's storage
    4) download <file>    fetch a file from the server onto the device …
    5) downloadcf         fetch the config file from the server …
    6) downloadfw         fetch a firmware image from the server …
    7) upload <file>      push a file from the device to the server …
  ! 8) reboot             restart the device
  ! 9) fsdcard            formats the storage card immediately — no confirmation …

    t) open the interactive terminal (stream_cli REPL) on 1001
    s) switch station
    q) quit

  (! = confirmation required)

  1001>
```

`--station 1001` skips the station picker. `t` drops you into the familiar REPL
(sections 2 and 3 below); `/quit` there returns **to the menu** rather than ending
the program.

### The command list comes from *this* file

The menu reads the table under "[Allowed device commands
(allowlist)](#allowed-device-commands-allowlist)" — verb, placeholder (`<file>`) and
description. **A new device command is therefore one row in that table and nothing
else**; it shows up in the menu without anyone touching `stream_menu.py`. Only that
one table is read (the parser keys on the heading text), so `--list` or `/stations`
never turn up as device commands. If the `.md` is missing — the script copied onto
another machine — the menu falls back to the verb list in `stream_cli.py`, then
without descriptions.

Two things deliberately live **in the code** rather than in this file's prose,
because a menu that infers danger from running text eventually formats a storage
card:

| In the code (`stream_menu.py`) | Effect |
|---|---|
| `CONFIRM` | `fsdcard` demands the typed word `FORMAT`, `reboot` a `y/N`. Marked `!` in the menu. |
| `NO_RESPONSE_EXPECTED` | `reboot` legitimately answers nothing (the device drops the connection) — the menu reports that as expected rather than as a timeout error. |

So a new **harmless** command grows into the menu by itself, while a new
**dangerous** one additionally needs a line in `CONFIRM`. That asymmetry is the
point.

Long output (`listfiles` can exceed 2000 lines) goes through the pager (`less`), so
the menu is still visible afterwards.

---

## 1. Which stations are online?

```bash
python stream_cli/stream_cli.py --list
```

Example output:

```
 station  gps_time                     pending  peer
    1001  2026-07-15T19:50:57            False  203.0.113.5:13930
    2201  2026-07-15T19:50:55            False  203.0.113.47:61529
(3 further station(s) known but not connected — use --list-all / '/stations all' to see them)
```

`--list` shows **only the actually connected** stations — those are the only ones a
command can reach. The server keeps its session per station id, though, and beyond
the end of the connection; such leftovers otherwise sit next to a live station with
`connected=False` as if they were equals, which invites sending into the void. The
footer only says how many there are.

To see them anyway (debugging: "which station was here last?"):

```bash
python stream_cli/stream_cli.py --list-all
```

```
 station  connected  gps_time                     pending  peer
    1001       True  2026-07-15T19:50:57            False  203.0.113.5:13930
    2201       True  2026-07-15T19:50:55            False  203.0.113.47:61529
    2001      False  None                           False  -
    1002      False  2026-07-14T12:14:21            False  -
```

Connected first, remembered ones after. The `connected` column exists only in this
view — in `--list` it would be constantly `True`.

| Column | Meaning |
|---|---|
| `station` | station id |
| `connected` | `True` = the TCP stream is currently connected (only with `--list-all`) |
| `gps_time` | the station's current GPS clock (empty before its first fix) |
| `pending` | `True` = a CLI request is in flight right now |
| `peer` | IP:port of the device connection (`-` when not connected) |

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

### Only connected stations can be opened

`--station` is **verified**, not believed. A station that is not connected no longer
opens a terminal — the server does keep its session per station id beyond the end of
the connection, but nothing behind it is reachable: every command would simply sit
there until the CLI timeout. Instead you get, immediately:

```
$ python stream_cli/stream_cli.py --station 1002
Station 1002 is known to the server but NOT connected — no command can reach it.
Connected right now: [1001, 2201]
 station  connected  gps_time                     pending  peer
    1001       True  2026-07-15T19:50:57            False  203.0.113.5:13930
    ...
```

An id the server has never heard of is distinguished from that (`is unknown to this
server (wrong instance, or it never connected)`) — that is usually the wrong instance
or the wrong admin port, not a device that dropped out. Either way the full station
list follows (as with `--list-all`) and the exit code is **2**; this holds for the
REPL and for one-shot mode alike.

The same check applies to `/station <id>` in the REPL. It queries the server
**afresh**, because the station may have dropped out (or come back) while the prompt
was waiting; if the check fails, the current station stays active:

```
1001> /station 1002
  ! station 1002 is known but not connected — staying on 1001
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
| `/stations` | re-list the connected stations |
| `/stations all` | additionally the remembered, not connected ones |
| `/station <id>` | switch the active station (only to a connected one) |
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
| `--station <id>` | station id; **must be connected** (omitted picks the sole connected one) |
| `--list` | list the **connected** stations and exit |
| `--list-all` | like `--list`, plus the remembered ones (`connected=False`) |
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
