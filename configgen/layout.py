#!/usr/bin/env python3
"""Read and write CONFIG.TXT layouts: prose, order and values kept apart.

WHY A LAYOUT AND NOT JUST A RENDERER
    The six role templates carry a lot of hand-written prose that is worth more
    than the keys it surrounds - the paragraph explaining that base and rover
    are opposite jobs has saved more time than any default value in the file.
    Generating templates from the schema alone would throw that away and
    replace it with something blander.

    So the template is split instead of rewritten: the prose and the ORDER live
    in a layout, the values are substitutable, and the schema says which keys
    a role may carry. Regenerating a template is then a replay, not an
    invention - and the config generator fills the same layout with a
    customer's values instead of the template's.

THE FORMAT THIS PARSES AND EMITS
    CRLF throughout. A line is
      - a section rule       --- Title ---
      - a comment            [anything]        (also # and ; are skipped by the
                                                firmware, but the templates use
                                                brackets)
      - an assignment        key = value       (padding after the key preserved)
      - blank
    Order does not matter to the firmware; it matters to the reader, so it is
    preserved exactly.

WHAT IT REFUSES TO EMIT
    The rules below are not style. Each one is a way a written file silently
    loses a value on a device (see configgen/reader.py):
      - a UTF-8 BOM        hides the FIRST key in the file
      - a tab after '='    ends up INSIDE the value
      - a value over 89 characters is truncated without a word
      - "key =" inside a comment shadows the real assignment on FW <= 1.58.1
        (BUG-046). Note the narrower rule: a bare mention like "station_mode
        decides everything below" is safe and the templates rely on it - it is
        the name followed by an equals sign that latches.
"""

import io
import re

CRLF = "\r\n"
MAX_VALUE = 89          # the firmware's guard is 90 source characters

# A section rule needs a TITLE between the dashes, and the title has to START
# with something that is neither a dash nor a space. A plain horizontal rule
# ("--------------") is otherwise read as a section whose name is more dashes,
# and comes back out as "--- -------- ---" - which is exactly how
# CONFIG_REFERENCE.TXT failed to round-trip.
RE_SECTION = re.compile(r"^---\s*([^-\s].*?)\s*---\s*$")
RE_KEY = re.compile(r"^([A-Za-z_0-9]+)([ \t]*)=[ \t]?(.*)$")


class LayoutError(Exception):
    pass


# --------------------------------------------------------------- parsing
def parse(text):
    """CONFIG.TXT text -> {title, rule, items}."""
    lines = text.split(CRLF)
    # Whether the file ends with a line terminator is part of the file. Some
    # older templates do not, and silently adding one makes every regenerated
    # file differ from its predecessor for no reason a reader can see.
    ends_with_newline = bool(lines) and lines[-1] == ""
    if ends_with_newline:
        lines.pop()

    title = rule = None
    start = 0
    if len(lines) >= 2 and set(lines[1].strip()) == {"-"} and lines[0].strip():
        title, rule, start = lines[0], lines[1], 2

    items = []
    for raw in lines[start:]:
        if not raw.strip():
            items.append({"t": "blank"})
            continue
        m = RE_SECTION.match(raw)
        if m:
            items.append({"t": "section", "title": m.group(1)})
            continue
        if raw.lstrip().startswith(("[", "#", ";")):
            if items and items[-1]["t"] == "comment":
                items[-1]["lines"].append(raw)
            else:
                items.append({"t": "comment", "lines": [raw]})
            continue
        m = RE_KEY.match(raw)
        if m:
            items.append({"t": "key", "key": m.group(1),
                          "pad": m.group(2), "value": m.group(3)})
            continue
        items.append({"t": "raw", "line": raw})
    return {"title": title, "rule": rule, "items": items,
            "ends_with_newline": ends_with_newline}


# --------------------------------------------------------------- rendering
def render(layout, values=None, drop_missing=False, rename=None, drop=None):
    """Layout -> CONFIG.TXT text.

    values      key -> replacement value, for producing a customer file from a
                template layout. Absent keys keep the layout's own value.
    drop_missing  omit key lines whose key is not in `values` (used when a role
                changes and a key no longer applies).
    rename      layout key -> the name to write instead. The documented way to
                switch a key off is to RENAME it, so switching one back ON is a
                rename too - the template ships the disabled spelling and this
                is what turns it into the live one.
    drop        layout keys whose line is left out entirely. An optional key the
                operator did not fill in is better absent than present-and-empty:
                absent is what the reference explicitly allows for the extra
                phone numbers, and it does not leave a stranger's value behind.
    """
    values = values or {}
    rename = rename or {}
    drop = drop or set()
    out = []
    if layout.get("title"):
        out.append(layout["title"])
        out.append(layout.get("rule") or "-" * len(layout["title"]))
    for it in layout["items"]:
        t = it["t"]
        if t == "blank":
            out.append("")
        elif t == "section":
            out.append("--- %s ---" % it["title"])
        elif t == "comment":
            out.extend(it["lines"])
        elif t == "raw":
            out.append(it["line"])
        elif t == "key":
            if it["key"] in drop:
                continue
            if drop_missing and it["key"] not in values:
                continue
            name = rename.get(it["key"], it["key"])
            # The padding belongs to the NAME, not to the line: a renamed key of
            # a different length would otherwise inherit spacing meant for the
            # old one, and for the two keys whose oldest firmware matched their
            # padding that changes what the device finds.
            pad = it["pad"] if name == it["key"] else " "
            v = values.get(it["key"], values.get(name, it["value"]))
            out.append("%s%s=%s" % (name, pad,
                                    "" if v == "" else " " + str(v)))
        else:
            raise LayoutError("unknown item type %r" % t)
    text = CRLF.join(out)
    return text + CRLF if layout.get("ends_with_newline", True) else text


# --------------------------------------------------------------- checking
def emit_findings(text, literal_keys=(), keys=None):
    """Everything that would make this text lose a value on a device.

    keys        the names the firmware actually reads. The comment check is
                restricted to these on purpose: prose legitimately contains
                things like "[RTCM reference station ID = DF003]" and
                "[0 = never upload, 1 = upload]", and flagging every word that
                happens to precede an equals sign buries the real finding under
                noise. Only a genuine key name can shadow a genuine key.
    literal_keys are the padded forms ("apn    =") that firmware up to 1.58.1
                matched including their spacing; a second occurrence of the
                exact form shadows the first.
    """
    findings = []
    if text.startswith("﻿"):
        findings.append("a UTF-8 BOM hides the first key in the file")
    if text.count("\n") != text.count(CRLF):
        findings.append("line endings are not CRLF throughout")

    for n, line in enumerate(text.split(CRLF), 1):
        m = RE_KEY.match(line)
        if not m:
            continue
        if "\t" in m.group(2) or m.group(3).startswith("\t"):
            findings.append("line %d: a tab around '=' lands inside the value" % n)
        if len(m.group(3)) > MAX_VALUE:
            findings.append("line %d: value is %d characters, the firmware keeps %d"
                            % (n, len(m.group(3)), MAX_VALUE))

    # BUG-046: "key =" written inside a comment latches on FW <= 1.58.1
    for n, line in enumerate(text.split(CRLF), 1):
        s = line.lstrip()
        if not s.startswith(("[", "#", ";")):
            continue
        for m in re.finditer(r"([A-Za-z_0-9]+)[ \t]*=", s):
            if keys is not None and m.group(1) not in keys:
                continue
            findings.append("line %d: %r inside a comment shadows the real "
                            "assignment on FW <= 1.58.1 - drop the underscores "
                            "in the prose" % (n, m.group(1) + " ="))
    for lit in literal_keys:
        if text.count(lit) > 1:
            findings.append("the literal %r occurs %d times; firmware up to "
                            "1.58.1 matched it including its padding"
                            % (lit, text.count(lit)))
    return findings


def read(path):
    return parse(io.open(path, encoding="utf-8", newline="").read())


def write(path, text):
    io.open(path, "w", encoding="utf-8", newline="").write(text)
