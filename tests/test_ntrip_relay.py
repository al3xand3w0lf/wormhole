"""Argument parsing for tools/ntrip_relay.py.

The parsing is the part worth testing: a mistyped target silently relaying
nowhere, or a password truncated at a colon, would look like a caster problem.
"""

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ntrip_relay import parse_args, parse_source, parse_target  # noqa: E402


def test_parse_source_splits_host_port_mount():
    assert parse_source("ntrip.example.org:2101/BASE1") == ("ntrip.example.org", 2101, "BASE1")


@pytest.mark.parametrize("value", [
    "ntrip.example.org/BASE1",       # no port
    "ntrip.example.org:2101",        # no mountpoint
    "ntrip.example.org:auto/BASE1",  # port not a number
])
def test_parse_source_rejects_incomplete_specs(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_source(value)


def test_parse_target_keeps_colons_inside_the_password():
    """A mountpoint cannot contain ":", so everything after its colon is password."""
    target = parse_target("127.0.0.1:2104/BASE1:pw:with:colons")
    assert (target.host, target.port, target.mountpoint) == ("127.0.0.1", 2104, "BASE1")
    assert target.password == "pw:with:colons"


def test_parse_target_reads_password_from_environment(monkeypatch):
    monkeypatch.setenv("BKG_ENCODER_PASSWORD", "from-env")
    assert parse_target("127.0.0.1:2104/BASE1:@BKG_ENCODER_PASSWORD").password == "from-env"


def test_parse_target_rejects_an_unset_environment_password(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(argparse.ArgumentTypeError):
        parse_target("127.0.0.1:2104/BASE1:@NOT_SET_ANYWHERE")


def test_parse_target_rejects_a_missing_password():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_target("127.0.0.1:2104/BASE1")


def test_several_targets_accumulate():
    args = parse_args([
        "--source", "ntrip.example.org:2101/BASE1",
        "--target", "127.0.0.1:2101/BASE1:pw1",
        "--target", "127.0.0.1:2104/BASE1:pw2",
    ])
    assert [t.port for t in args.target] == [2101, 2104]
    # Defaults: casters that ignore the pull credentials still get a non-empty one.
    assert args.source_pass == "none"
