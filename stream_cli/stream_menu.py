#!/usr/bin/env python3
"""
stream_menu.py — menu front-end for the streaming server's remote CLI.

A launcher around `stream_cli.py`, not a replacement for it: pick a station from a
list, pick a command from a list, see the output, repeat. `stream_cli.py` keeps
working exactly as before, and this file reuses it wholesale — the admin HTTP calls,
the config resolution, the timeouts and the REPL all come from there, so there is no
second copy of any of it to drift.

    python stream_cli/stream_menu.py                 # station menu, then command menu
    python stream_cli/stream_menu.py --station 1001  # straight to the command menu

The command list is READ FROM THE DOCUMENTATION: the allowlist table in
`stream_cli_commands.md`. A command the firmware gains is therefore a row in that
table and nothing else — it appears in the menu with its description, no code change.
`stream_cli.ALLOWLIST` is the fallback for a copy of this script that was moved away
from the .md.

Two things are deliberately *not* taken from the doc, because reading intent out of
prose is how a menu ends up formatting an SD card by accident:

  * CONFIRM — which commands must be confirmed, and how hard. Code, not prose.
  * NO_RESPONSE_EXPECTED — commands whose silence is correct rather than a timeout.

Adding a *dangerous* command therefore does need a line here. That asymmetry is the
point: the harmless path grows by itself, the harmful one does not.

Python standard library only, like stream_cli.py.
"""

import argparse
import pydoc
import re
import shutil
import sys
import urllib.error
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stream_cli as cli  # noqa: E402 - needs the path insert above


COMMANDS_MD = Path(__file__).resolve().parent / "stream_cli_commands.md"

# The allowlist table lives under this heading; every other table in the file
# (meta-commands, CLI options) is off limits — restricting the parse to the section
# is what keeps `--list` or `/stations` from turning up as device commands.
MD_SECTION = "### Allowed device commands"

# | `download <file>` | fetch a file **from the server onto the device** |
MD_ROW = re.compile(r"^\|\s*`([a-z0-9_]+)([^`]*)`\s*\|\s*(.+?)\s*\|\s*$")

# Commands that must be confirmed before they are sent. value = the exact word the
# operator has to type, or None for a plain y/N. `fsdcard` formats the SD card
# immediately and without asking the device side — this prompt is the only guard
# that exists anywhere in the chain.
CONFIRM = {
    "fsdcard": "FORMAT",
    "reboot": None,
}

# Commands that legitimately answer nothing: the device drops the TCP connection and
# reboots instead of sending a CLI_RESPONSE, so the client timeout is the expected
# outcome, not a fault to report as one.
NO_RESPONSE_EXPECTED = {"reboot"}


def strip_markdown(text: str) -> str:
    """Table cell -> one plain line: no bold, no links, no leading warning glyphs."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\[(.+?)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("⚠️", "").replace("**", "")
    return " ".join(text.split())


def load_commands() -> list[tuple[str, str, str]]:
    """(verb, arg placeholder, description) for every row of the allowlist table.

    Falls back to stream_cli.ALLOWLIST — verbs only, no descriptions — when the .md
    is absent, which is the "script copied onto another host" case."""
    commands: list[tuple[str, str, str]] = []
    if COMMANDS_MD.is_file():
        in_section = False
        for line in COMMANDS_MD.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                in_section = line.startswith(MD_SECTION)
                continue
            if not in_section:
                continue
            m = MD_ROW.match(line)
            if m:
                verb, arg, desc = m.group(1), m.group(2).strip(), strip_markdown(m.group(3))
                commands.append((verb, arg, desc))
    if not commands:
        commands = [(verb, "", "") for verb in cli.ALLOWLIST]
    return commands


def show_output(text: str) -> None:
    """Print the response, through the pager when it would scroll off the screen.

    `listfiles` on a full SD card is >2000 lines; dumping that into the scrollback
    right before the menu redraws pushes the menu out of view."""
    if not text.strip():
        print("  (no output)")
        return
    height = shutil.get_terminal_size((80, 24)).lines
    if len(text.splitlines()) > height - 4:
        pydoc.pager(text)
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def ask(prompt: str) -> Optional[str]:
    """input() that treats Ctrl-C/Ctrl-D as 'go back', not as 'crash'."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def confirm(verb: str, station: int) -> bool:
    word = CONFIRM.get(verb, "")
    if verb not in CONFIRM:
        return True
    if word is None:
        answer = ask(f"  Really send '{verb}' to station {station}? [y/N] ")
        return (answer or "").lower() in ("y", "yes", "j", "ja")
    print(f"  !! '{verb}' on station {station} is destructive and takes effect")
    print(f"     immediately — the device does NOT ask again.")
    answer = ask(f"  Type {word} to proceed (anything else aborts): ")
    return answer == word


def station_label(s: dict) -> str:
    return (f"{s['station_id']:<6} gps {str(s.get('gps_time') or '-'):<21} "
            f"{s.get('peer') or '-'}")


def choose_station(ctx, preset: Optional[int]) -> Optional[int]:
    """Station menu. Only connected stations are selectable — same rule as
    stream_cli's --station guard: nothing else can be reached."""
    if preset is not None:
        stations = cli.fetch_stations(ctx)
        if cli.is_connected(stations, preset):
            return preset
        print(f"Station {preset} is not connected.", file=sys.stderr)

    show_all = False
    while True:
        try:
            stations = cli.fetch_stations(ctx)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            print(f"  ! {e}", file=sys.stderr)
            return None
        connected = [s for s in stations if s.get("connected")]
        offline = [s for s in stations if not s.get("connected")]

        print(f"\n  Stations on {cli.ADMIN_URL}\n")
        for i, s in enumerate(connected, 1):
            print(f"   {i:>2}) {station_label(s)}")
        if not connected:
            print("   (none connected — start the device, then reload)")
        if show_all and offline:
            print("\n   not connected (cannot be selected):")
            for s in offline:
                print(f"       {station_label(s)}")
        print()
        if offline and not show_all:
            print(f"    a) also show the {len(offline)} station(s) that are known "
                  f"but not connected")
        print("    r) reload")
        print("    q) quit\n")

        choice = ask("  Station> ")
        if choice is None or choice.lower() in ("q", "quit", "exit"):
            return None
        if choice.lower() == "r":
            continue
        if choice.lower() == "a":
            show_all = True
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(connected):
            return connected[int(choice) - 1]["station_id"]
        print("  ! not a listed number", file=sys.stderr)


def run_command(station: int, verb: str, arg_hint: str, ctx) -> None:
    cmd = verb
    if arg_hint:
        value = ask(f"  {verb} {arg_hint} — value (empty aborts): ")
        if not value:
            return
        cmd = f"{verb} {value}"
    if not confirm(verb, station):
        print("  aborted.")
        return

    print(f"\n  -> {cmd} @ {station}")
    response = cli.send_cmd(station, cmd, ctx, None)
    if response is None:
        if verb in NO_RESPONSE_EXPECTED:
            print(f"  (no answer — expected for '{verb}': the device drops the "
                  f"connection instead of replying)")
        return
    show_output(response)


def command_menu(station: int, ctx) -> bool:
    """Command menu for one station. Returns True to go back to the station menu,
    False to leave the program."""
    commands = load_commands()
    width = max(len(v) + len(a) + 1 for v, a, _ in commands) + 2
    while True:
        # The .md descriptions are prose and can be a paragraph long (fsdcard's is);
        # a wrapped menu row is unreadable, so cut to the terminal instead.
        columns = shutil.get_terminal_size((80, 24)).columns
        room = max(20, columns - width - 8)
        print(f"\n  Station {station} — commands\n")
        for i, (verb, arg, desc) in enumerate(commands, 1):
            marker = "!" if verb in CONFIRM else " "
            label = f"{verb} {arg}".strip()
            if len(desc) > room:
                desc = desc[:room - 1].rstrip() + "…"
            print(f"  {marker}{i:>2}) {label:<{width}} {desc}")
        print(f"\n    t) open the interactive terminal (stream_cli REPL) on {station}")
        print("    s) switch station")
        print("    q) quit")
        print("\n  (! = confirmation required)\n")

        choice = ask(f"  {station}> ")
        if choice is None or choice.lower() in ("q", "quit", "exit"):
            return False
        if choice.lower() == "s":
            return True
        if choice.lower() == "t":
            cli.repl(station, ctx, None)
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(commands):
            verb, arg, _ = commands[int(choice) - 1]
            run_command(station, verb, arg, ctx)
            continue
        print("  ! not a listed number", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Menu front-end for the streaming server's remote CLI.",
        epilog="Commands are read from stream_cli_commands.md; the admin URL and API "
               "key are resolved exactly as in stream_cli.py (flag > env > .env).",
    )
    parser.add_argument("--station", type=int, default=None,
                        help="Skip the station menu (must be connected)")
    parser.add_argument("--url", default=None, help="Admin base URL (default: from .env)")
    parser.add_argument("--api-key", default=None, help="X-API-Key (default: from .env)")
    args = parser.parse_args()

    cli.resolve_config(args.url, args.api_key)
    ctx = cli.ssl_context()

    try:
        cli.fetch_stations(ctx)
    except urllib.error.HTTPError as e:
        print(f"FAILED: HTTP {e.code} {e.reason} (check API key)", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"FAILED: could not reach {cli.ADMIN_URL}: {e.reason}", file=sys.stderr)
        return 1

    preset = args.station
    while True:
        station = choose_station(ctx, preset)
        preset = None
        if station is None:
            return 0
        if not command_menu(station, ctx):
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
