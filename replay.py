#!/usr/bin/env python3
"""Replay a recorded raw capture through the live demux pipeline.

The raw captures written by the server (`<station>/raw/*.bin`) are byte-exact
recordings of the TCP stream. Feeding them back through the *same* framer, router
and sinks lets us test decoders and reprocess history without any hardware — and,
once NTRIP / live-UBX consumers exist, re-derive their inputs from the archive.

Usage:
    python replay.py data/incoming_stream/1001/raw/*.bin --out ./replayed
    python replay.py data/incoming_stream/1001/raw --out ./replayed
"""

import argparse
import sys
from pathlib import Path

from streaming import stationdir
from streaming.framer import StreamFramer
from streaming.pipeline import ident_station_id, is_ident, route_frame
from streaming.sinks import FileSink
from streaming.station import StationSession

CHUNK = 65536


def expand(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.bin")))
        else:
            paths.append(p)
    return [p for p in paths if p.is_file()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a raw stream capture")
    parser.add_argument("inputs", nargs="+", help="raw .bin file(s) or a directory")
    parser.add_argument("--out", required=True, help="output root directory")
    parser.add_argument(
        "--station",
        type=int,
        help="station id to assume if the capture contains no IDENT frame",
    )
    parser.add_argument("--no-raw", action="store_true", help="do not re-write the raw capture")
    args = parser.parse_args()

    files = expand(args.inputs)
    if not files:
        print("no input files found", file=sys.stderr)
        return 1

    out_root = Path(args.out)
    framer = StreamFramer()
    session: StationSession | None = None

    def start(station_id: int) -> StationSession:
        sinks = [FileSink(out_root, station_id, raw_capture=not args.no_raw)]
        return StationSession(station_id, sinks)

    if args.station is not None:
        session = start(args.station)

    total = 0
    for path in files:
        print(f"replaying {path} ...")
        with open(path, "rb") as fh:
            while True:
                data = fh.read(CHUNK)
                if not data:
                    break
                total += len(data)
                for frame in framer.feed(data):
                    if session is None:
                        if not is_ident(frame):
                            continue
                        station_id = ident_station_id(frame)
                        if station_id is None:
                            continue
                        session = start(station_id)
                        print(f"  station {station_id} identified")
                        continue
                    route_frame(session, frame)

                if session is not None and not args.no_raw:
                    for sink in session.sinks:
                        sink.on_raw(data)

    if session is None:
        print(
            "no IDENT frame found - pass --station <id> to replay an anonymous capture",
            file=sys.stderr,
        )
        return 1

    session.close()
    print(
        f"\n{total} bytes | ubx={session.ubx_frames} rtcm3={session.rtcm3_frames} "
        f"private={session.private_frames} "
        f"resyncs={framer.resync_events} garbage={framer.garbage_bytes} B"
    )
    print(f"output: {stationdir.resolve(out_root, session.station_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
