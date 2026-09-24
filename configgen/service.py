"""Import, validate and generate a device CONFIG.TXT.

ONE CODE PATH
    The web form, the API and the command line all go through the functions
    here. A fix to the padding rules, the line endings or the shadow detection
    can therefore not land in one and miss the other - which is the failure the
    split between a "quick CLI" and a "proper UI" usually produces.

GENERATION IS ALWAYS FROM THE ROLE TEMPLATE
    Importing an existing file extracts VALUES only; the output is rendered
    fresh from the role's template layout. This is a decision, not laziness:

      - the prose in the templates is worth more than the customer's copy of it,
        and it is kept current in one place instead of in every station's file
      - every generated file then has correct line endings, correct padding for
        the keys whose oldest firmware matched their spacing, and no shadowing
        defect - none of which a patch-in-place writer can promise once
        arbitrary customer comments are preserved
      - a customer file may itself CARRY a shadowing defect. Preserving its
        comments would carry the cause forward even after fixing the value.

    The cost is that a customer's own notes do not survive. That is stated
    plainly in the import findings rather than hidden.
"""

import io
import re

from . import layout, reader
from .schema import MANDATORY, NOT_APPLICABLE

ERROR = "error"
WARNING = "warning"
INFO = "info"


def _finding(level, key, text):
    return {"level": level, "key": key, "message": text}


# --------------------------------------------------------------- validation
def validate_value(entry, value):
    """None if the value is acceptable, else why not."""
    if value == "":
        return None                      # "not set" is always allowed
    t = entry["type"]
    if t == "bool01":
        return None if value in ("0", "1") else "must be 0 or 1"
    if t == "int":
        if not re.fullmatch(r"-?\d+", value):
            return "must be a whole number"
        rng = entry.get("range")
        if rng and not (rng[0] <= int(value) <= rng[1]):
            return "must be between %d and %d" % (rng[0], rng[1])
        return None
    if t == "float":
        return None if re.fullmatch(r"-?\d+(\.\d+)?", value) else "must be a number"
    if t == "enum":
        allowed = entry.get("enum", {})
        if value in allowed or value in allowed.values():
            return None
        return "must be one of: %s" % ", ".join(sorted(allowed))
    if t == "time_hhmm":
        return None if re.fullmatch(r"\d{1,2}:\d{2}", value) else "must be HH:MM"
    if t == "offset_hhmm":
        return None if re.fullmatch(r":?\d{1,2}(:\d{2})?", value) \
            else "must be MM, :MM or HH:MM"
    max_len = entry.get("max_len")
    if max_len and len(value) > max_len:
        return "at most %d characters" % max_len
    if len(value) > layout.MAX_VALUE:
        return "at most %d characters - the device truncates beyond that" \
            % layout.MAX_VALUE
    return None


def validate(schema, role, values):
    """Findings for a proposed configuration. Errors block, warnings do not."""
    findings = []
    if role not in schema.roles:
        return [_finding(ERROR, "station_mode", "unknown role %r" % role)]

    for key, value in sorted(values.items()):
        entry = schema.by_key.get(schema.canonical(key))
        if entry is None:
            if key in schema.disabled_spellings:
                # The documented way to switch a key off is to RENAME it -
                # bracketing does not disable it on older firmware. The renamed
                # form is therefore correct, not an unknown key, and the role
                # templates ship carrying it.
                findings.append(_finding(
                    INFO, key, "a deliberately disabled %s"
                    % schema.disabled_spellings[key]))
            else:
                findings.append(_finding(WARNING, key,
                                         "not a key this firmware reads"))
            continue
        why = validate_value(entry, str(value))
        if why:
            findings.append(_finding(ERROR, key, why))
        elif entry["roles"].get(role) == NOT_APPLICABLE and str(value) != "":
            findings.append(_finding(
                INFO, key,
                "has no effect in role %s - the device reads it and ignores it" % role))

    for key in sorted(schema.mandatory_for(role)):
        if str(values.get(key, "")).strip() == "":
            findings.append(_finding(
                ERROR, key,
                "required in role %s - without it the device loses its persistent "
                "backup copy of the configuration" % role))

    findings.extend(_check_upload_target(schema, role, values))

    # station_mode and operation_mode must agree: firmware old enough not to
    # know the role key reads only the numeric one, so a mismatch runs an older
    # unit in the wrong role without saying so.
    sm, om = str(values.get("station_mode", "")), str(values.get("operation_mode", ""))
    if sm and om:
        want = "0" if sm in ("batch", "batch_duty") else "1"
        if om != want:
            findings.append(_finding(
                ERROR, "operation_mode",
                "role %s needs operation_mode %s" % (sm, want)))
    if sm and sm != role:
        findings.append(_finding(
            ERROR, "station_mode",
            "value %r does not match the selected role %r" % (sm, role)))
    return findings


def _check_upload_target(schema, role, values):
    """The gap the firmware's own check leaves open.

    Whether the FTP keys are mandatory is decided by the ROLE alone
    (cfgUploadKeysRequired), never by upload_protocol - so they stay required
    even for a station that uploads over HTTP, and leaving them out costs it its
    persistent config backup. The other half of that asymmetry is worse: the
    whole http_* family is NEVER counted, in any role. A batch station set to
    HTTP with no server passes every check the device makes and then has nowhere
    to put its files.

    The device cannot notice that. This can.
    """
    findings = []
    section = next((s for s in schema.sections if s["id"] == "upload"), None)
    choice = (section or {}).get("choice")
    if not choice:
        return findings
    # Only in the roles where the upload block means anything at all - the
    # streaming roles have no HTTP and no FTP path.
    if schema.state("server_ftp", role) == NOT_APPLICABLE:
        return findings
    if str(values.get("enable_upload", "")).strip() == "0":
        return findings

    selected = str(values.get(choice["key"], "")).strip()
    group = choice["groups"].get(selected)
    if group is None:
        return findings

    label = choice.get("labels", {}).get(group, group)
    for entry in schema.keys:
        if entry.get("group") != group:
            continue
        # A selected target needs an address; the ports and endpoints have
        # workable defaults and are not worth nagging about.
        if entry["key"] not in ("http_server", "server_ftp"):
            continue
        # A key the role already makes mandatory is reported by that check;
        # saying it twice in different words reads like two problems.
        if schema.state(entry["key"], role) == MANDATORY:
            continue
        if str(values.get(entry["key"], "")).strip() == "":
            findings.append(_finding(
                ERROR, entry["key"],
                "%s is the selected upload target, so this is where the files go "
                "- the device never reports it as missing, it simply has nowhere "
                "to upload" % label))
    return findings


# --------------------------------------------------------------- defaults
def read_template(schema, role, template=None):
    """Read a template preserving its CRLF exactly.

    newline="" is load-bearing: without it Python translates the line endings on
    the way in, the layout replay is no longer byte-identical, and every
    generated file quietly differs from the template it came from.

    `template` overrides the role's default layout. A role can have more than one
    - a base either surveys its own position or is told where it stands, and the
    two read very differently - and without this the second one is unreachable,
    which is an invitation to render a file by hand and lose every check that
    lives in generate().
    """
    path = template if template is not None else schema.template_path(role)
    with io.open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def defaults_for(schema, role, template=None):
    """The role template's own values - the starting point for a new file."""
    text = read_template(schema, role, template)
    lay = layout.parse(text)
    return {it["key"]: it["value"] for it in lay["items"] if it["t"] == "key"}


def form_defaults(schema, role, template=None):
    """defaults_for(), minus the values that belong to a switched-off key.

    A key switched off by renaming carries whatever the template happens to hold
    - for the extra phone numbers that is a real number belonging to somebody.
    Handing it to the form as a starting value would put it into a customer's
    file the moment nobody looked. The live field starts empty instead, which is
    the truth: the key is off.
    """
    values = defaults_for(schema, role, template)
    for entry in schema.keys:
        values.pop(entry.get("disabled_spelling"), None)
    return values


def apply_prefill(schema, role, values, prefill):
    """Merge this installation's own settings, but only where they belong.

    A prefilled value for a key the role ignores is not merely useless - it is
    a secret written into a file that had no reason to carry it. The batch
    API key in a streaming rover's configuration is the concrete case: the
    generator would emit it, the device would read and discard it, and the key
    would sit on a removable card for no purpose at all.
    """
    for key, value in prefill.items():
        entry = schema.by_key.get(schema.canonical(key))
        if entry is None or entry["roles"].get(role) == NOT_APPLICABLE:
            continue
        values[key] = value
    return values


# --------------------------------------------------------------- import
def import_text(schema, text):
    """Extract values from an existing file, and say what is wrong with it.

    Returns (role, values, findings). The role is derived the way the device
    derives it, so an old file without the role key is still placed correctly.
    """
    findings = []
    if text.startswith("﻿"):
        findings.append(_finding(
            WARNING, None,
            "the file starts with a byte-order mark, which hides its FIRST key "
            "from the device - the generated file will not have one"))
        text = text.lstrip("﻿")
    if text.count("\n") != text.count("\r\n"):
        findings.append(_finding(
            WARNING, None,
            "line endings are not CRLF throughout - older firmware can merge "
            "two lines; the generated file is CRLF"))

    # what the DEVICE would read, not what a convenient regex would
    values = {}
    for entry in schema.keys:
        v = reader.read_value(text, entry["key"])
        if v is None:
            for alias in entry.get("aliases", ()):
                v = reader.read_value(text, alias)
                if v is not None:
                    break
        if v is not None:
            values[entry["key"]] = v

    # a shadowed key means the station was running the comment's value
    for key, hit in reader.shadow_check(text, list(schema.by_key)).items():
        findings.append(_finding(
            WARNING, key,
            "line %d mentions this key in a comment above the real assignment on "
            "line %d. On older firmware the comment wins, so the station may have "
            "been running %r instead of %r. The value shown is the one the device "
            "would read." % (hit["comment_line"], hit["assignment_line"],
                             hit["comment_value"], hit["assignment_value"])))

    written = reader.assignments(text)
    for name in sorted(set(written)):
        canon = schema.canonical(name)
        if canon in schema.by_key:
            continue
        if name in schema.disabled_spellings:
            findings.append(_finding(
                INFO, name,
                "a deliberately disabled key (renamed from %s)"
                % schema.disabled_spellings[name]))
            continue
        obs = schema.obsolete.get(name)
        if obs:
            when = obs.get("removed_fw")
            findings.append(_finding(
                INFO, name,
                "no longer read by the firmware%s. %s"
                % (" as of %s" % when if when else "", obs.get("note", ""))))
            values.pop(name, None)
            continue
        findings.append(_finding(
            WARNING, name,
            "not a key this firmware reads; it will not appear in the "
            "generated file"))

    role = derive_role(schema, values)
    if not values.get("station_mode"):
        findings.append(_finding(
            INFO, "station_mode",
            "the file has no role key; the role was derived from operation_mode "
            "and rtcm_client the way the firmware derives it, giving %r" % role))

    findings.append(_finding(
        INFO, None,
        "the generated file is rendered fresh from the %s template, so any notes "
        "written into this file are not carried over" % role))

    # Keys the file did not set will be written with the TEMPLATE's value, and
    # for anything that states a fact about the hardware that is a claim nobody
    # made. The near-miss was `antenna`: an old file without the key would have
    # been regenerated carrying the template's antenna model, which then travels
    # into the header of every log that station writes. Naming them is cheap;
    # guessing which ones matter is not, so all of them are listed.
    try:
        template = defaults_for(schema, role)
    except Exception:                                        # noqa: BLE001
        template = {}
    inherited = sorted(k for k, v in template.items()
                       if k not in values and str(v).strip() != "")
    if inherited:
        findings.append(_finding(
            INFO, None,
            "these keys are absent from the file and will be written with the %s "
            "template's value - check any that describe this station rather than "
            "configure it: %s" % (role, ", ".join(inherited))))
    return role, values, findings


def derive_role(schema, values):
    """The firmware's own derivation, so an import cannot disagree with a device.

    An explicit role key wins outright. Without it the role comes from
    operation_mode plus rtcm_client - a pair that cannot express the plain
    streaming role at all, because 'base' is what you got by not asking. That
    gap is why the role key exists; reproducing the gap is what makes the
    import faithful rather than merely plausible.
    """
    explicit = str(values.get("station_mode", "")).strip()
    if explicit in schema.roles:
        return explicit
    if str(values.get("operation_mode", "0")).strip() != "1":
        return "batch"
    return "rover" if str(values.get("rtcm_client", "0")).strip() == "1" else "base"


# --------------------------------------------------------------- generate
def generate(schema, role, values, template=None):
    """Render the file. Returns (text, findings). Errors do not stop rendering -
    the operator is shown both, so a file can be inspected before it is fixed.

    `template` picks a different layout for the same role - see read_template().
    """
    findings = validate(schema, role, values)

    text = read_template(schema, role, template)
    lay = layout.parse(text)

    known = {it["key"] for it in lay["items"] if it["t"] == "key"}
    supplied = {schema.canonical(k): str(v) for k, v in values.items()}
    # keep the role consistent even if the caller did not pass it
    supplied.setdefault("station_mode", role)
    supplied.setdefault("operation_mode",
                        "0" if role in ("batch", "batch_duty") else "1")

    # Keys that are switched off by RENAMING them (the extra phone numbers).
    #
    # The templates ship the disabled spelling, so a value typed into the live
    # field never matched the template's line and was silently dropped - the
    # generated file kept the template's number instead of the one the operator
    # entered. Filling the field now renames the line back to the live key;
    # leaving it empty removes the line altogether, which the reference
    # explicitly allows for these keys and which is the only way not to hand a
    # customer a number that is not theirs.
    rename, drop = {}, set()
    for entry in schema.keys:
        disabled = entry.get("disabled_spelling")
        if not disabled:
            continue
        live = entry["key"]
        wanted = str(supplied.get(live, "")).strip()
        if wanted:
            if disabled in known:
                rename[disabled] = live
                supplied[disabled] = wanted
        else:
            # Only keep the template's own disabled value when the caller passed
            # it back deliberately - that is how a template reproduces itself.
            if not str(supplied.get(disabled, "")).strip():
                drop.add(disabled)
                drop.add(live)

    out = layout.render(lay, supplied, rename=rename, drop=drop)

    dropped = sorted(k for k in supplied
                     if k not in known and k in schema.by_key
                     and schema.state(k, role) != NOT_APPLICABLE
                     and supplied[k] != "")
    for key in dropped:
        findings.append(_finding(
            INFO, key,
            "set, but the %s template has no line for it, so it was not written"
            % role))

    for problem in layout.emit_findings(out, literal_keys=schema.literal_forms,
                                        keys=set(schema.by_key) | set(schema.aliases)):
        findings.append(_finding(ERROR, None, "generated file: %s" % problem))
    return out, findings


def has_errors(findings):
    return any(f["level"] == ERROR for f in findings)
