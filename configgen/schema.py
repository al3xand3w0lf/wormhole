"""The device configuration schema: what keys exist, and what a role needs.

The schema is DATA, vendored from the firmware repository that owns it. This
module only reads it. Nothing here knows any
key name - add a key to the firmware and to the schema, and the API, the web
form and the CLI all pick it up with no change here.

THE ONE THING WORTH KNOWING BEFORE USING IT
    A key's applicability to a role has THREE states, not two:

      mandatory  the device counts its absence as a config error, and a config
                 error skips the persistent backup copy of the configuration -
                 so a station missing one still runs, but has lost the copy it
                 falls back to when its card cannot be read
      optional   the key has an effect in this role but is never counted
      n/a        the key is meaningless in this role

    'n/a' does NOT mean rejected. Real stations in the field carry whole blocks
    of keys their role ignores - the firmware reads them harmlessly - and an
    importer that treated that as an error would refuse working configurations.
    The role templates omit them by convention; that is a style rule for
    templates, not a validity rule for files.
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SCHEMA_PATH = DATA_DIR / "device_config.schema.json"
TEMPLATE_DIR = DATA_DIR / "templates"

MANDATORY = "mandatory"
OPTIONAL = "optional"
NOT_APPLICABLE = "n/a"


class Schema:
    def __init__(self, raw):
        self.raw = raw
        self.keys = raw["keys"]
        self.roles = raw["roles"]
        self.sections = raw["sections"]
        self.by_key = {k["key"]: k for k in self.keys}
        self.aliases = {a: k["key"] for k in self.keys for a in k.get("aliases", ())}
        self.obsolete = {e["key"]: e for e in raw.get("obsolete_keys", ())}
        self.disabled_spellings = {
            k["disabled_spelling"]: k["key"]
            for k in self.keys if "disabled_spelling" in k
        }
        # padded forms the oldest firmware matched including their spacing
        self.literal_forms = tuple(
            k["display"] + "=" for k in self.keys if k["display"] != k["key"])

    @property
    def firmware(self):
        return self.raw.get("schema_for_firmware", "unknown")

    @property
    def device_name(self):
        return self.raw.get("device_name", "device")

    def canonical(self, name):
        """Fold an alias onto the key it is a spelling of."""
        return self.aliases.get(name, name)

    def state(self, key, role):
        entry = self.by_key.get(key)
        return entry["roles"].get(role) if entry else None

    def keys_for_role(self, role, include_na=False):
        out = []
        for k in self.keys:
            st = k["roles"].get(role)
            if st == NOT_APPLICABLE and not include_na:
                continue
            out.append(k)
        return out

    def mandatory_for(self, role):
        return {k["key"] for k in self.keys if k["roles"].get(role) == MANDATORY}

    def section_titles(self):
        return {s["id"]: s["title"] for s in self.sections}

    def template_path(self, role):
        return TEMPLATE_DIR / ("%s.TXT" % role)

    def as_form_spec(self):
        """Everything the browser needs to draw the form, and nothing else.

        The schema's own help text travels with each field, which is what makes
        the page's reference section and the form the same source.

        `secret` is carried as metadata, not as an instruction to hide: the form
        shows every value in clear text. Masking protected nothing - the value
        is in the preview, the download and on the card anyway - while making a
        token impossible to check by eye.
        """
        return {
            "device": self.device_name,
            "firmware": self.firmware,
            "roles": self.roles,
            "sections": self.sections,
            "keys": [
                {
                    "key": k["key"],
                    "section": k["section"],
                    "type": k["type"],
                    "default": k["default"],
                    "unit": k.get("unit"),
                    "range": k.get("range"),
                    "enum": k.get("enum"),
                    "max_len": k.get("max_len"),
                    "secret": k["type"] == "secret",
                    "help": k.get("help", ""),
                    "roles": k["roles"],
                    "group": k.get("group"),
                }
                for k in self.keys
            ],
            "obsolete": list(self.obsolete.values()),
        }


_cached = None


def load(path=None):
    """Load the schema once; it does not change while the process runs."""
    global _cached
    if path is not None:
        return Schema(json.loads(Path(path).read_text(encoding="utf-8")))
    if _cached is None:
        _cached = Schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    return _cached
