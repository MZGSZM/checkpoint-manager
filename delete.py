"""Safe delete and trash move. The only module that removes anything.

Every path is re-verified against the whitelist (spec 8.1) immediately
before it is touched, not just at scan time.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from scan import CATEGORIES, Snapshot, is_link_or_junction, measure_path, parse_snapshot_name

LOG_RE = re.compile(r"^checkpoint_\d{8}\.log$")


class SafetyError(Exception):
    """A path failed the deletion whitelist."""


@dataclass
class Failure:
    key: str
    error: str


@dataclass
class RunResult:
    deleted: list[str] = field(default_factory=list)
    failed: list[Failure] = field(default_factory=list)
    bytes_reclaimed: int = 0
    moved_to: str | None = None


def _resolved(p: Path) -> Path:
    return Path(os.path.realpath(p))


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def verify_snapshot_path(root: Path, path: Path) -> None:
    """Raise SafetyError unless `path` is a deletable snapshot folder.

    root/<saves|extdata>/<title>/<snapshot>, snapshot name parses, no link
    anywhere in the chain, resolved path inside the resolved root.
    """
    root = Path(root)
    path = Path(path)
    title_dir = path.parent
    cat_dir = title_dir.parent
    if cat_dir.name not in CATEGORIES:
        raise SafetyError(f"{path}: not inside saves/ or extdata/")
    if _resolved(cat_dir.parent) != _resolved(root):
        raise SafetyError(f"{path}: category folder is not a direct child of the root")
    if parse_snapshot_name(path.name) is None:
        raise SafetyError(f"{path}: name is not a snapshot name")
    for p in (path, title_dir, cat_dir):
        if is_link_or_junction(p):
            raise SafetyError(f"{p}: is a link or junction")
    if not path.is_dir():
        raise SafetyError(f"{path}: not a folder (or no longer exists)")
    real = _resolved(path)
    real_root = _resolved(root)
    if real == real_root or not _inside(real, real_root):
        raise SafetyError(f"{path}: resolves outside the root")
    if real.parent.parent.parent != real_root:
        raise SafetyError(f"{path}: wrong depth below the root")


def verify_log_path(root: Path, path: Path) -> None:
    """Raise SafetyError unless `path` is root/logs/checkpoint_YYYYMMDD.log."""
    root = Path(root)
    path = Path(path)
    if path.parent.name != "logs" or _resolved(path.parent.parent) != _resolved(root):
        raise SafetyError(f"{path}: not inside the root's logs/ folder")
    if not LOG_RE.match(path.name):
        raise SafetyError(f"{path}: not a checkpoint_YYYYMMDD.log file")
    if is_link_or_junction(path) or is_link_or_junction(path.parent):
        raise SafetyError(f"{path}: is a link")
    if not path.is_file():
        raise SafetyError(f"{path}: not a file (or no longer exists)")
    if not _inside(_resolved(path), _resolved(root)):
        raise SafetyError(f"{path}: resolves outside the root")


def _nearest_existing(p: Path) -> Path:
    p = Path(os.path.abspath(p))
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def verify_trash_dir(root: Path, trash: Path) -> Path:
    """The trash folder must not be on the SD card (spec 8.7)."""
    trash = Path(trash).expanduser()
    real_root = _resolved(root)
    if _inside(_resolved(_nearest_existing(trash)), real_root) or _inside(Path(os.path.abspath(trash)), real_root):
        raise SafetyError(f"Trash folder {trash} is inside the Checkpoint folder")
    try:
        if _same_device(_nearest_existing(trash), Path(root)):
            raise SafetyError(
                f"Trash folder {trash} is on the same drive as the card. "
                "Pick a folder on the PC, since moving within the card frees nothing."
            )
    except OSError as e:
        raise SafetyError(f"Cannot check trash folder {trash}: {e.strerror or e}") from None
    return trash


def _same_device(a: Path, b: Path) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def _long_path(p: Path) -> str:
    """Windows extended-length prefix for deep paths."""
    s = os.path.abspath(p)
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
        return "\\\\?\\" + s
    return s


def _rmtree(path: Path) -> None:
    """rmtree that clears the read-only attribute and retries once (spec 8.5)."""

    def handler(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            raise exc

    if sys.version_info >= (3, 12):
        shutil.rmtree(_long_path(path), onexc=handler)
    else:
        shutil.rmtree(_long_path(path), onerror=lambda f, p, ei: handler(f, p, ei[1]))


def _explain(e: BaseException) -> str:
    if isinstance(e, OSError):
        if e.errno == errno.EROFS:
            return "card is read-only (check the write-protect switch on the SD adapter)"
        if e.errno in (errno.ENOENT, errno.ENODEV, errno.EIO):
            return f"{e.strerror or e} (card removed?)"
        return e.strerror or str(e)
    return str(e)


def _unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    n = 1
    while True:
        cand = dest.with_name(f"{dest.name}-{n}")
        if not cand.exists():
            return cand
        n += 1


def delete_snapshots(root: Path, snaps: list[Snapshot], trash_dir: Path | None = None,
                     progress: Callable[[int, int, Snapshot], None] | None = None) -> RunResult:
    """Delete (or move to trash) each snapshot. One failure never stops the rest."""
    result = RunResult(moved_to=str(trash_dir) if trash_dir else None)
    if trash_dir is not None:
        try:
            trash_dir = verify_trash_dir(root, trash_dir)
        except SafetyError as e:
            result.failed = [Failure(s.key, str(e)) for s in snaps]
            return result

    total = len(snaps)
    for i, snap in enumerate(snaps):
        if progress:
            progress(i, total, snap)
        try:
            verify_snapshot_path(root, snap.path)
            size = snap.size_bytes
            if size is None:
                size, _, _ = measure_path(snap.path)
            if trash_dir is not None:
                dest = _unique_dest(Path(trash_dir) / snap.category / snap.title_dir / snap.name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(_long_path(snap.path), _long_path(dest), symlinks=True)
                verify_snapshot_path(root, snap.path)
                _rmtree(snap.path)
            else:
                _rmtree(snap.path)
            result.deleted.append(snap.key)
            result.bytes_reclaimed += size
        except SafetyError as e:
            result.failed.append(Failure(snap.key, f"refused: {e}"))
        except Exception as e:  # keep going on any error
            result.failed.append(Failure(snap.key, _explain(e)))
    if progress:
        progress(total, total, None)
    return result


def delete_logs(root: Path, paths: list[Path]) -> RunResult:
    result = RunResult()
    for p in paths:
        key = f"logs/{p.name}"
        try:
            verify_log_path(root, p)
            size = p.stat().st_size
            try:
                os.remove(_long_path(p))
            except PermissionError:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
                os.remove(_long_path(p))
            result.deleted.append(key)
            result.bytes_reclaimed += size
        except SafetyError as e:
            result.failed.append(Failure(key, f"refused: {e}"))
        except Exception as e:
            result.failed.append(Failure(key, _explain(e)))
    return result
