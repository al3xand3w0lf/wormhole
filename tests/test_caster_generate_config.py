"""caster/generate_config.py with no stations yet.

A fresh install does not necessarily know its base station ids. With
STREAM_CASTER_AUTO_ENABLE the server provisions each base itself, so the caster
has to be able to start empty; without it an empty caster would stay empty, and
that is still refused.
"""

import importlib.util
from pathlib import Path

import pytest

GENERATE = Path(__file__).resolve().parent.parent / "caster" / "generate_config.py"


@pytest.fixture
def gen(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("generate_config", GENERATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(mod, "ETC_DIR", tmp_path / "etc")
    monkeypatch.setattr(mod, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(mod, "REPO_DIR", tmp_path)
    return mod


def test_empty_stations_with_auto_provision_writes_an_empty_caster(gen, tmp_path):
    (tmp_path / ".env").write_text(
        "STREAM_CASTER_STATIONS=\nSTREAM_CASTER_AUTO_ENABLE=true\nSTREAM_CASTER_PORT=12002\n")
    gen.main()

    etc = tmp_path / "etc"
    assert "port: 12002" in (etc / "caster.yaml").read_text()
    assert "STR;" not in (etc / "sourcetable.dat").read_text()
    assert (etc / "source.auth").read_text().strip() == ""
    # The server needs the push enabled, or the first auto-provisioned base
    # gets a mountpoint but no sink until the next restart.
    assert "STREAM_CASTER_ENABLE=true" in (tmp_path / ".env").read_text()


def test_empty_stations_without_auto_provision_is_still_refused(gen, tmp_path):
    (tmp_path / ".env").write_text("STREAM_CASTER_STATIONS=\n")
    with pytest.raises(SystemExit):
        gen.main()
    assert not (tmp_path / "etc").exists()
