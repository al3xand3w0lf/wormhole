"""Generate device configuration files from the command line.

For rolling out a fleet, and for scripting. Not a second user interface: it
calls the same import/validate/generate functions the web form calls, so a fix
to the line endings, the padding or the shadow detection cannot land in one and
miss the other.

    # one station, starting from the role template
    python3 -m configgen --role ntrip_rover --set station=A002 -o CONFIG.TXT

    # start from an existing file and change one thing
    python3 -m configgen --from old/CONFIG.TXT --set apn=gprs.example -o new.TXT

    # a whole fleet from a CSV whose header names the keys
    python3 -m configgen --roster stations.csv --out-dir ./generated/

Exits non-zero if any file has errors, so it is safe in a deploy script.
"""

import argparse
import csv
import io
import sys
from pathlib import Path

from . import service
from .schema import load as load_schema


def read_text(path):
    with io.open(path, encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def write_text(path, text):
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def show(findings, prefix="  "):
    for f in findings:
        sys.stderr.write("%s[%-7s] %s%s\n"
                         % (prefix, f["level"],
                            (f["key"] + ": ") if f["key"] else "", f["message"]))


def env_prefill():
    """This installation's own settings, when run on the server itself."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from streaming import config
        from .router import _prefill
        values, _ = _prefill(config)
        return values
    except Exception:                                        # noqa: BLE001
        return {}


def one(schema, role, values, out_path, quiet=False):
    text, findings = service.generate(schema, role, values)
    errors = service.has_errors(findings)
    if not quiet:
        sys.stderr.write("%s  role=%s  %s\n"
                         % (out_path or "(stdout)", role,
                            "ERRORS" if errors else "ok"))
        show(findings)
    if out_path:
        write_text(out_path, text)
    else:
        sys.stdout.write(text)
    return not errors


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="configgen", description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", help="role for a new file")
    ap.add_argument("--from", dest="source", metavar="FILE",
                    help="start from an existing configuration file")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a value (repeatable)")
    ap.add_argument("--roster", metavar="CSV",
                    help="one row per station; the header names the keys, and a "
                         "'role' column may override --role")
    ap.add_argument("-o", "--out", metavar="FILE", help="write here (default stdout)")
    ap.add_argument("--out-dir", metavar="DIR",
                    help="with --roster: one <station>.TXT per row")
    ap.add_argument("--no-prefill", action="store_true",
                    help="do not fill in this installation's own settings")
    ap.add_argument("--list-roles", action="store_true")
    args = ap.parse_args(argv)

    schema = load_schema()
    if args.list_roles:
        for r in schema.roles:
            print("%-12s %2d required keys" % (r, len(schema.mandatory_for(r))))
        return 0

    overrides = {}
    for item in args.set:
        if "=" not in item:
            ap.error("--set expects KEY=VALUE, got %r" % item)
        k, v = item.split("=", 1)
        overrides[k.strip()] = v.strip()

    prefill = {} if args.no_prefill else env_prefill()

    # ---------------------------------------------------------------- roster
    if args.roster:
        if not args.out_dir:
            ap.error("--roster needs --out-dir")
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ok = True
        with io.open(args.roster, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            sys.stderr.write("roster is empty\n")
            return 1
        for row in rows:
            role = (row.pop("role", None) or args.role or "").strip()
            if role not in schema.roles:
                sys.stderr.write("row %r: unknown or missing role %r\n"
                                 % (row.get("station"), role))
                ok = False
                continue
            values = dict(service.defaults_for(schema, role))
            service.apply_prefill(schema, role, values, prefill)
            values.update({k: v for k, v in row.items()
                           if k and v is not None and v != ""})
            values.update(overrides)
            name = values.get("station") or "station"
            ok &= one(schema, role, values, out_dir / ("%s.TXT" % name))
        return 0 if ok else 1

    # ---------------------------------------------------------------- single
    if args.source:
        role, values, findings = service.import_text(schema, read_text(args.source))
        show(findings)
        if args.role:
            role = args.role
        base = service.defaults_for(schema, role)
        base.update(values)
        values = base
    elif args.role:
        role = args.role
        values = dict(service.defaults_for(schema, role))
        service.apply_prefill(schema, role, values, prefill)
    else:
        ap.error("give --role, --from or --roster")

    if role not in schema.roles:
        ap.error("unknown role %r; known: %s" % (role, ", ".join(schema.roles)))
    values.update(overrides)
    return 0 if one(schema, role, values, args.out) else 1


if __name__ == "__main__":
    sys.exit(main())
