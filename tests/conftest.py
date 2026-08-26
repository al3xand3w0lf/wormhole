import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_caster_env(monkeypatch):
    """streaming/config.py loads the real .env at import time, so this box's
    live STREAM_CASTER_* values (actual caster hosts/ports/passwords) are
    already in os.environ before any test runs. Tests that build a
    CasterTarget straight from a name (test_ntrip.py's _caster_targets()
    calls) rely on the documented default (127.0.0.1:2101) for whatever they
    don't set themselves - strip the real ones first so a target added to
    production .env can't silently change what a test's "unset" means.
    """
    for key in list(os.environ):
        if key.startswith("STREAM_CASTER_"):
            monkeypatch.delenv(key, raising=False)
