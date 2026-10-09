import os
from datetime import datetime

import pytest

import scan


@pytest.mark.parametrize("name, tid, display", [
    ("0x00308 MARIO KART 7", "0x00308", "MARIO KART 7"),
    ("0xF700E Super Mario World", "0xF700E", "Super Mario World"),
    ("0x00C9B Pokémon Bank", "0x00C9B", "Pokémon Bank"),
    ("0x012EC Tetris® Ultimate", "0x012EC", "Tetris® Ultimate"),
    ("0x019BD Mario Party  Star Rush", "0x019BD", "Mario Party  Star Rush"),
    ("0x01D14 Mario & Luigi  Bowser's Inside…", "0x01D14", "Mario & Luigi  Bowser's Inside…"),
    ("DSPI HOMEBREW", None, "DSPI HOMEBREW"),
    ("0x0100000000010000 Super Mario Odyssey", "0x0100000000010000", "Super Mario Odyssey"),
    ("01006F8002326000 Animal Crossing", "01006F8002326000", "Animal Crossing"),
    ("FACE Off", None, "FACE Off"),
    ("ABBA Gold", None, "ABBA Gold"),
    ("DEADBEE Seven", None, "DEADBEE Seven"),
])
def test_parse_title_dir(name, tid, display):
    assert scan.parse_title_dir(name) == (tid, display)


@pytest.mark.parametrize("name, expected", [
    ("20261008-144633", (datetime(2026, 10, 8, 14, 46, 33), "")),
    ("20250813-214417 before boss", (datetime(2025, 8, 13, 21, 44, 17), " before boss")),
    ("20261008-144727 Player1", (datetime(2026, 10, 8, 14, 47, 27), " Player1")),
    ("notes", None),
    ("2026100-144633", None),
    ("20251399-999999", None),
    ("x20261008-144633", None),
])
def test_parse_snapshot_name(name, expected):
    assert scan.parse_snapshot_name(name) == expected


def test_load_small(small):
    root = scan.load_root(small)
    assert root.platform == "3ds"
    assert root.categories == ["saves", "extdata"]
    by_key = {t.key: t for t in root.titles}
    theme = by_key["extdata/0x0008F Theme"]
    assert len(theme.snapshots) == 24
    assert theme.snapshots[0].name == "20261008-145355"   # newest first
    assert [s.timestamp for s in theme.snapshots] == sorted((s.timestamp for s in theme.snapshots), reverse=True)
    # Same title in both categories, independent histories
    assert len(by_key["saves/0x00308 MARIO KART 7"].snapshots) == 12
    assert len(by_key["extdata/0x00308 MARIO KART 7"].snapshots) == 12
    # Empty titles are listed
    assert by_key["saves/DSPI HOMEBREW"].snapshots == []
    assert by_key["saves/DSPI HOMEBREW"].title_id is None
    assert by_key["extdata/0x0137E NSMB 2  Gold Edition"].snapshots == []
    # Label kept and shown
    star = by_key["saves/0x019BD Mario Party  Star Rush"]
    assert [s.label for s in star.snapshots] == [" before boss", ""]
    # Unrecognised entries are reported, never modelled as snapshots
    skipped = {p.path.name for p in root.skipped}
    assert {"notes", "20251399-999999", "readme.txt", "stray.bin"} <= skipped
    names = {s.name for s in root.all_snapshots()}
    assert "notes" not in names and "20251399-999999" not in names


def test_measure(small):
    root = scan.load_root(small)
    scan.measure_all(root)
    theme = root.title_by_key("extdata/0x0008F Theme")
    assert all(s.file_count == 3 for s in theme.snapshots)
    assert theme.snapshots[0].size_bytes == 5768 + 5000 + 1168
    assert theme.total_size == 24 * (5768 + 5000 + 1168)


def test_resolve_root_variants(small, tmp_path):
    sd = small.parent.parent
    assert scan.resolve_root(small) == small
    assert scan.resolve_root(sd) == small
    with pytest.raises(scan.RootError):
        scan.resolve_root(tmp_path / "missing")
    with pytest.raises(scan.RootError):
        scan.resolve_root(small / "saves")


def test_resolve_ambiguous(small):
    import fixtures
    sd = small.parent.parent
    fixtures.build_switch(sd)
    with pytest.raises(scan.AmbiguousRootError):
        scan.resolve_root(sd)
    assert scan.resolve_root(sd, "switch") == sd / "switch" / "Checkpoint"
    assert scan.resolve_root(sd, "3ds") == small


def test_root_needs_marker(tmp_path):
    (tmp_path / "Checkpoint" / "saves").mkdir(parents=True)
    with pytest.raises(scan.RootError):
        scan.resolve_root(tmp_path / "Checkpoint")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinks_not_followed(small, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.sav").write_text("do not touch")
    os.symlink(outside, small / "saves" / "0x00308 MARIO KART 7" / "20270101-000000")
    os.symlink(outside, small / "saves" / "0x99999 Linked Title")
    root = scan.load_root(small)
    names = {s.name for s in root.all_snapshots()}
    assert "20270101-000000" not in names
    assert "saves/0x99999 Linked Title" not in {t.key for t in root.titles}
    reasons = {p.path.name: p.reason for p in root.skipped}
    assert "link" in reasons["20270101-000000"]


def test_switch_layout(switch):
    root = scan.load_root(switch)
    assert root.platform == "switch"
    assert root.categories == ["saves"]
    ids = {t.display_name: t.title_id for t in root.titles}
    assert ids == {"Super Mario Odyssey": "0x0100000000010000", "Animal Crossing": "01006F8002326000",
                   "FACE Off": None, "ABBA Gold": None}
    face = next(t for t in root.titles if t.dir_name == "FACE Off")
    assert face.snapshots[0].label == " Player1"
    assert any(p.path.name == "Player1" for p in root.skipped)


def test_format_size():
    assert scan.format_size(None) == "?"
    assert scan.format_size(0) == "0 B"
    assert scan.format_size(1023) == "1023 B"
    assert scan.format_size(1536) == "1.5 KB"
    assert scan.format_size(5 * 1024 ** 3) == "5.0 GB"
