"""Station labels: name + id -> directory, and back again."""

import pytest

from streaming import stationdir


@pytest.mark.parametrize("raw, expected", [
    ("A001", "A001"),
    ("T010", "T010"),
    ("Zurich Nord", "Zurich_Nord"),
    ("Zürich", "Zurich"),          # folded to ASCII, not blanked to "Z_rich"
    ("a/b", "a_b"),
    ("../etc", "etc"),             # no traversal survives
    ("  ", ""),                    # nothing usable -> caller falls back to the id
    ("___", ""),
    ("x" * 60, "x" * 31),          # device buffer is char[32]
])
def test_sanitize(raw, expected):
    assert stationdir.sanitize(raw) == expected


def test_label_keeps_the_id_as_the_anchor():
    """The name is free text, editable in the field and not unique. Two sites
    both called "Test" must not share a directory, so the id stays in the
    label - and it is what resolve() matches on."""
    assert stationdir.label(2001, "A001") == "A001_2001"
    assert stationdir.label(2001, "Test") == "Test_2001"
    assert stationdir.label(2002, "Test") == "Test_2002"


def test_label_without_a_name_is_the_old_layout():
    """Pre-1.69 firmware sends no name. Its archive path must not change."""
    assert stationdir.label(2001, "") == "2001"
    assert stationdir.label(2001, "  ") == "2001"


def test_resolve_prefers_a_labelled_directory(tmp_path):
    (tmp_path / "A001_2001").mkdir()
    assert stationdir.resolve(tmp_path, 2001) == tmp_path / "A001_2001"


def test_resolve_falls_back_to_the_bare_id(tmp_path):
    (tmp_path / "2001").mkdir()
    assert stationdir.resolve(tmp_path, 2001) == tmp_path / "2001"


def test_resolve_does_not_confuse_stations_with_a_shared_id_suffix(tmp_path):
    """Station 1 must not match "A001_2001" just because the name ends in _1."""
    (tmp_path / "A001_2001").mkdir()
    assert stationdir.resolve(tmp_path, 1) == tmp_path / "1"


def test_resolve_on_an_empty_root_returns_the_bare_id(tmp_path):
    assert stationdir.resolve(tmp_path, 2001) == tmp_path / "2001"


def test_adopt_migrates_an_existing_bare_id_directory(tmp_path):
    """A station that gains a name must not end up split across "2001/" (its
    history) and "A001_2001/" (everything new) - that is the exact ambiguity
    this feature removes."""
    old = tmp_path / "2001" / "ubx"
    old.mkdir(parents=True)
    (old / "2001_ubx_20260901_10.ubx").write_bytes(b"x")

    got = stationdir.adopt(tmp_path, 2001, "A001")

    assert got == tmp_path / "A001_2001"
    assert (got / "ubx" / "2001_ubx_20260901_10.ubx").read_bytes() == b"x"
    assert not (tmp_path / "2001").exists()


def test_adopt_never_merges_into_an_existing_label(tmp_path):
    """If both directories somehow exist, the rename is skipped rather than
    risking a merge or a clobber. The labelled one wins; the bare one is left
    on disk for a human to look at."""
    (tmp_path / "2001").mkdir()
    (tmp_path / "A001_2001").mkdir()

    assert stationdir.adopt(tmp_path, 2001, "A001") == tmp_path / "A001_2001"
    assert (tmp_path / "2001").is_dir()


def test_adopt_without_a_name_is_a_no_op(tmp_path):
    (tmp_path / "2001").mkdir()
    assert stationdir.adopt(tmp_path, 2001, "") == tmp_path / "2001"
    assert (tmp_path / "2001").is_dir()
