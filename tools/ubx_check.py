#!/usr/bin/env python3
"""Walk a raw byte dump and check every UBX frame's checksum.

Companion to GNSS_TUNNEL_DUMP_DIR in streaming/gnss_tunnel.py. Answers one
question and only one: are the bytes that reached this side intact?

Why it exists: on 2026-09-22 the device and the server agreed on the byte COUNT
to the byte (3736 = 3736, zero drops) while ubxfwupdate reported
"Packet (CLSID 06-41): CRC-error" on the receiver's answers. Counts prove
nothing about content. This reads the content.

    python tools/ubx_check.py data/tunnel_dumps/tunnel_1020_up.bin

Output: how many frames carried a valid checksum, how many did not, and the
first bad one in hex - the class/id of a corrupted frame usually says which
exchange it belonged to.
"""
import sys
from collections import Counter

SYNC = b"\xb5\x62"


def ubx_checksum(payload: bytes) -> tuple[int, int]:
    """Fletcher-8 over class, id, length and payload - UBX §3.4."""
    ck_a = ck_b = 0
    for b in payload:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b


def main(path: str) -> int:
    data = open(path, "rb").read()
    print(f"{path}: {len(data)} B\n")

    ok = bad = 0
    truncated = 0
    by_id_ok: Counter = Counter()
    by_id_bad: Counter = Counter()
    first_bad = None
    consumed = 0          # bytes that sat inside a frame
    i = 0

    while True:
        j = data.find(SYNC, i)
        if j < 0:
            break
        if j + 6 > len(data):
            truncated += 1
            break
        cls, mid = data[j + 2], data[j + 3]
        length = data[j + 4] | (data[j + 5] << 8)
        end = j + 6 + length + 2
        if end > len(data):
            truncated += 1
            break

        body = data[j + 2 : j + 6 + length]
        ck_a, ck_b = ubx_checksum(body)
        name = f"{cls:02X}-{mid:02X}"
        if (ck_a, ck_b) == (data[end - 2], data[end - 1]):
            ok += 1
            by_id_ok[name] += 1
            consumed += end - j
            i = end                      # continue after a good frame
        else:
            bad += 1
            by_id_bad[name] += 1
            if first_bad is None:
                first_bad = (j, data[j : min(end, j + 64)])
            # Do NOT trust the length of a frame that failed its checksum:
            # resynchronise on the next sync pattern instead.
            i = j + 2

    total = ok + bad
    print(f"UBX frames: {total}   valid {ok}   INVALID {bad}   truncated at end: {truncated}")
    if total:
        inside = 100.0 * consumed / len(data)
        print(f"bytes inside valid frames: {consumed} ({inside:.1f} %)")
    print()

    if by_id_ok:
        print("valid   :", ", ".join(f"{k}x{v}" for k, v in by_id_ok.most_common(12)))
    if by_id_bad:
        print("INVALID :", ", ".join(f"{k}x{v}" for k, v in by_id_bad.most_common(12)))
    if first_bad:
        off, blob = first_bad
        print(f"\nfirst bad frame at offset {off}:")
        print("  " + " ".join(f"{b:02X}" for b in blob))

    if bad == 0 and total > 0:
        print("\n==> Every UBX frame on this side carries a valid checksum.")
        print("    Corruption, if any, happened AFTER this point.")
    elif bad:
        print("\n==> Frames are already corrupt here.")
        print("    Corruption happened at or before this point (receiver -> device -> server).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
