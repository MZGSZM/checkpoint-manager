import os

import pytest

import delete as deleter
import scan
from conftest import tree


def snap(root, key):
    cat, title, name = key.split("/")
    model = scan.load_root(root)
    t = model.title_by_key(f"{cat}/{title}")
    return next(s for s in t.snapshots if s.name == name)


THEME_OLD = "extdata/0x0008F Theme/20250520-213146"


def test_verify_accepts_real_snapshot(small):
    deleter.verify_snapshot_path(small, small / THEME_OLD)


@pytest.mark.parametrize("rel", [
    "extdata/0x0008F Theme",                         # title folder
    "extdata",                                       # category folder
    "logs",
    "config.json",
    "saves/0x00308 MARIO KART 7/notes",              # name does not parse
    "saves/0x00308 MARIO KART 7/20251399-999999",     # invalid date
    "saves/0x00308 MARIO KART 7/readme.txt",
    "saves/0x00308 MARIO KART 7/20250520-212712/replay",   # too deep
    "scripts/universal",
    "saves/0x00308 MARIO KART 7/20259999-000000",     # does not exist
])
def test_verify_refuses(small, rel):
    with pytest.raises(deleter.SafetyError):
        deleter.verify_snapshot_path(small, small / rel)


def test_verify_refuses_outside_root(small, tmp_path):
    other = tmp_path / "other" / "saves" / "0x00001 X" / "20250101-000000"
    other.mkdir(parents=True)
    with pytest.raises(deleter.SafetyError):
        deleter.verify_snapshot_path(small, other)
    with pytest.raises(deleter.SafetyError):
        deleter.verify_snapshot_path(small, small / "saves" / ".." / ".." / "x" / "20250101-000000")


def test_verify_refuses_deep_title_trick(small):
    # saves/<title>/saves/<title>/<snapshot>: right names, wrong depth
    trick = small / "saves" / "0x00308 MARIO KART 7" / "saves" / "0x1 T" / "20250101-000000"
    trick.mkdir(parents=True)
    with pytest.raises(deleter.SafetyError):
        deleter.verify_snapshot_path(small, trick)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_verify_refuses_symlinks(small, tmp_path):
    outside = tmp_path / "outside" / "20250101-000000"
    outside.mkdir(parents=True)
    (outside / "keep.sav").write_text("keep")
    link = small / "saves" / "0x00308 MARIO KART 7" / "20270101-000000"
    os.symlink(outside, link)
    with pytest.raises(deleter.SafetyError):
        deleter.verify_snapshot_path(small, link)
    # Linked title folder containing a real-looking snapshot
    os.symlink(outside.parent, small / "saves" / "0x77777 Linked")
    with pytest.raises(deleter.SafetyError):
        deleter.verify_snapshot_path(small, small / "saves" / "0x77777 Linked" / "20250101-000000")
    assert (outside / "keep.sav").exists()


def test_delete_removes_only_targets(small):
    before = tree(small)
    s = snap(small, THEME_OLD)
    result = deleter.delete_snapshots(small, [s])
    assert result.deleted == [THEME_OLD] and not result.failed
    assert result.bytes_reclaimed == 5768 + 5000 + 1168
    gone = before - tree(small)
    assert all(g == THEME_OLD or g.startswith(THEME_OLD + "/") for g in gone)
    assert (small / "extdata" / "0x0008F Theme").is_dir()   # title folder stays


def test_one_failure_does_not_stop_the_rest(small):
    a = snap(small, THEME_OLD)
    b = snap(small, "extdata/0x0008F Theme/20250520-213152")
    import shutil
    shutil.rmtree(a.path)   # vanished before deletion
    result = deleter.delete_snapshots(small, [a, b])
    assert result.deleted == [b.key]
    assert [f.key for f in result.failed] == [a.key]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="root ignores permissions")
def test_read_only_reported(small):
    s = snap(small, THEME_OLD)
    title_dir = s.path.parent
    os.chmod(title_dir, 0o555)
    try:
        result = deleter.delete_snapshots(small, [s])
    finally:
        os.chmod(title_dir, 0o755)
    assert result.failed and not result.deleted


def test_read_only_files_are_cleared(small):
    s = snap(small, THEME_OLD)
    for f in s.path.iterdir():
        os.chmod(f, 0o444)
    result = deleter.delete_snapshots(small, [s])
    assert result.deleted == [s.key]


def test_trash_on_same_device_refused(small, tmp_path):
    with pytest.raises(deleter.SafetyError):
        deleter.verify_trash_dir(small, tmp_path / "trash")
    with pytest.raises(deleter.SafetyError):
        deleter.verify_trash_dir(small, small / "trash")
    result = deleter.delete_snapshots(small, [snap(small, THEME_OLD)], tmp_path / "trash")
    assert result.failed and (small / THEME_OLD).exists()


def test_trash_move(small, tmp_path, monkeypatch):
    monkeypatch.setattr(deleter, "_same_device", lambda a, b: False)
    trash = tmp_path / "trash"
    s = snap(small, THEME_OLD)
    result = deleter.delete_snapshots(small, [s], trash)
    assert result.deleted == [s.key]
    assert not s.path.exists()
    moved = trash / "extdata" / "0x0008F Theme" / "20250520-213146"
    assert sorted(p.name for p in moved.iterdir()) == ["Cache.dat", "CacheD.dat", "SaveData.dat"]
    # A second snapshot with the same name gets a suffix, nothing overwritten
    (small / THEME_OLD).mkdir()
    (small / THEME_OLD / "again").write_text("x")
    deleter.delete_snapshots(small, [snap(small, THEME_OLD)], trash)
    assert (moved.parent / "20250520-213146-1" / "again").exists()
    # Inside the card is still refused even if the device check passes
    with pytest.raises(deleter.SafetyError):
        deleter.verify_trash_dir(small, small / "trash")


def test_logs(small):
    ok = small / "logs" / "checkpoint_20250427.log"
    deleter.verify_log_path(small, ok)
    for bad in (small / "logs" / "other.txt", small / "config.json", small / "logs"):
        with pytest.raises(deleter.SafetyError):
            deleter.verify_log_path(small, bad)
    result = deleter.delete_logs(small, [ok, small / "logs" / "other.txt"])
    assert result.deleted == ["logs/checkpoint_20250427.log"]
    assert len(result.failed) == 1
    assert (small / "logs" / "other.txt").exists()
