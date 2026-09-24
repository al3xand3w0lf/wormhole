"""Read a device CONFIG.TXT exactly the way the device's parser reads it.

WHY NOT A REGEX
    The obvious `^key\\s*=\\s*(.*)$` is wrong in a way that does not announce
    itself: `\\s*` crosses a newline, so a key written with an empty value
    swallows whatever line comes next. A file containing

        streaming_cli_secret =

        --- Downlink: NTRIP caster ---

    reads back with the section heading as the secret. The device does not do
    this - its reader stops at the line ending - so an importer that used the
    convenient regex would show the operator values their station never had.
    This module is a line-by-line port of the firmware's two functions instead,
    and the port is pinned by tests.

WHAT THE FIRMWARE ACTUALLY DOES
    locate(): walks the buffer a line at a time. Lines whose first non-blank
    character is '[', '#', ';' or '-' are comments and are skipped entirely -
    that is what makes it safe to document a key directly above it. The key
    match must be followed by whitespace or '=' so that `phone_number` does not
    also match `phone_number2`.

    read_value(): from just past the '=', copies to the END OF LINE, dropping
    ' ', ';' and '='. It keeps a TAB, and it stops after 90 source characters -
    both silently. Those two are quirks, not features, and the writer side
    refuses to emit either (see layout.emit_findings).
"""

MAX_SOURCE_CHARS = 90
COMMENT_PREFIXES = "[#;-"
DROPPED = " ;="


def locate(buf, key):
    """Index just past the '=' of `key`'s assignment, or None."""
    i = 0
    n = len(buf)
    while i < n:
        eol = buf.find("\n", i)
        line_end = n if eol < 0 else eol
        p = i
        while p < line_end and buf[p] in " \t":
            p += 1
        if p < line_end and buf[p] in COMMENT_PREFIXES:
            i = line_end + 1
            continue
        if buf.startswith(key, p):
            q = p + len(key)
            if q < line_end and buf[q] in " \t=":
                while q < line_end and buf[q] in " \t":
                    q += 1
                if q < line_end and buf[q] == "=":
                    return q + 1
        i = line_end + 1
    return None


def read_value(buf, key):
    """The value the device would read, or None if the key is absent."""
    p = locate(buf, key)
    if p is None:
        return None
    out = []
    guard = 0
    while p < len(buf) and buf[p] not in "\r\n" and guard < MAX_SOURCE_CHARS:
        if buf[p] not in DROPPED:
            out.append(buf[p])
        p += 1
        guard += 1
    return "".join(out)


def assignments(buf):
    """Every key that has an assignment line, in file order.

    Used to spot names the schema does not know. Deliberately separate from
    locate(): this is "what is written in the file", locate() is "what the
    device would find", and on a file with a shadowing defect the two differ -
    which is the finding, not a bug.
    """
    import re
    seen = []
    for line in buf.split("\n"):
        s = line.lstrip()
        if not s or s[0] in COMMENT_PREFIXES:
            continue
        m = re.match(r"^([A-Za-z_0-9]+)[ \t]*=", s)
        if m:
            seen.append(m.group(1))
    return seen


def shadow_check(buf, keys):
    """Keys whose name appears in a comment ABOVE their assignment.

    On firmware old enough to search the whole file rather than line by line,
    the comment wins and the value is parsed out of the prose. That defect cost
    a base station its entire function once; a file carrying it must be
    reported, not silently corrected, because the device may have been running
    the shadowed value for a long time.
    """
    import re
    findings = {}
    lines = buf.split("\n")
    for key in keys:
        comment_hit = None
        for n, line in enumerate(lines, 1):
            s = line.lstrip()
            if s.startswith(tuple(COMMENT_PREFIXES)):
                m = re.search(r"\b%s[ \t]*=[ \t]*([^\]\r\n]*)" % re.escape(key), s)
                if m and comment_hit is None:
                    comment_hit = (n, m.group(1).strip())
            elif re.match(r"^%s[ \t]*=" % re.escape(key), s):
                if comment_hit is not None:
                    findings[key] = {
                        "comment_line": comment_hit[0],
                        "comment_value": comment_hit[1],
                        "assignment_line": n,
                        "assignment_value": s.split("=", 1)[1].strip(),
                    }
                break
    return findings
