"""Station directory naming: id, name, and the label built from both.

Why this exists
---------------
A station has two identities. The **streaming station id** (`streaming_station_id`,
e.g. 2001) is what the device puts on the wire and what every runtime mechanism —
caster mountpoint, rover subscription, admin API — is keyed on. The **station
name** (`station_name` in CONFIG.TXT, e.g. "A001") is what the project calls the
site, and it is the identity the batch-mode file names carried for years.

Until FW 1.69.x the stream carried only the id, so a station reconfigured from
batch to base lost its name at the door and post-processing could no longer tell
that 2001's data belongs to A001. IDENT now carries the name too, and the archive
is written under a *label* that contains both:

    A001_2001/  ->  A001_2001_ubx_20260901_12.ubx

The id stays in the label on purpose. The name is free text, editable in the
field and not guaranteed unique; the id is the anchor that keeps two sites called
"Test" from writing into the same directory, and it is what `resolve()` below
matches on. A station whose IDENT carries no name (pre-1.69 firmware) keeps the
bare `2001/` layout exactly as before.

The device sends the name RAW — spaces, umlauts, whatever is in CONFIG.TXT. The
sanitising happens here, once, so that "what the station is called" and "what is
safe as a path component" never get confused with each other.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Everything outside this set becomes '_'. Deliberately narrow: this string ends
# up in directory names, file names and glob patterns on any host that ever
# touches the archive.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Mirrors the device's char station_name[32] -> at most 31 characters.
MAX_NAME_LEN = 31


def sanitize(name: str) -> str:
    """Turn a raw device-supplied station name into a safe path component.

    Returns "" when nothing usable survives — the caller then falls back to the
    bare id, which is always valid.
    """
    if not name:
        return ""
    # Umlauts and accents fold to ASCII rather than to '_': "Zürich" should
    # become "Zurich", not "Z_rich".
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    safe = _UNSAFE.sub("_", folded).strip("._-")
    safe = re.sub(r"_{2,}", "_", safe)
    return safe[:MAX_NAME_LEN]


def label(station_id: int, name: str = "") -> str:
    """The directory / file-name label for a station: "A001_2001", or "2001"."""
    safe = sanitize(name)
    return f"{safe}_{station_id}" if safe else str(station_id)


def resolve(root: Path, station_id: int) -> Path:
    """Find the on-disk directory of a station, whatever label it was given.

    Readers (caster sourcetable, baseline analysis, replay) know only the id.
    Prefers a labelled directory `*_<id>`; falls back to the bare `<id>`, which
    is also what is returned when the station has no directory yet.
    """
    bare = root / str(station_id)
    if root.is_dir():
        suffix = f"_{station_id}"
        matches = sorted(
            p for p in root.iterdir()
            if p.is_dir() and p.name.endswith(suffix) and p.name != suffix
        )
        if matches:
            return matches[0]
    return bare


def adopt(root: Path, station_id: int, name: str) -> Path:
    """Return the station's directory, migrating a bare `<id>` dir onto the label.

    Called once per station per process, when its sinks are built. Without the
    rename a station that gains a name would keep its history in `2001/` and
    write everything new into `A001_2001/` — the same station split across two
    directories, which is exactly the ambiguity this feature exists to remove.

    The rename is skipped (and the existing target used) if the target already
    exists, so it can never merge two directories or destroy anything.
    """
    target = root / label(station_id, name)
    bare = root / str(station_id)
    if target != bare and bare.is_dir() and not target.exists():
        bare.rename(target)
        return target
    return target
