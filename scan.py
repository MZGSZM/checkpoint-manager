"""Root discovery, parsing and the Title/Snapshot model.

Read-only. Nothing in this module writes to or deletes from the card.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

CATEGORIES = ("saves", "extdata")

# Leading timestamp, anything after it is a label (spec 3.2).
SNAPSHOT_RE = re.compile(r"^(\d{8}-\d{6})(.*)$", re.DOTALL)

# 0x prefix with any number of hex digits, or 8+ bare hex digits (spec 3.5).
TITLE_ID_RE = re.compile(r"^(0x[0-9A-Fa-f]+|[0-9A-Fa-f]{8,}) (.+)$", re.DOTALL)

# Files that must never be written or deleted (spec 5.1).
READ_ONLY_FILES = ("config.json", "titles.sha", "fullsavecache", "fullextdatacache")


class RootError(Exception):
    """The given path is not usable as a Checkpoint root."""


class AmbiguousRootError(RootError):
    """An SD root holds both 3ds/Checkpoint and switch/Checkpoint."""

    def __init__(self, message: str, candidates: dict[str, Path]):
        super().__init__(message)
        self.candidates = candidates


@dataclass(frozen=True)
class Snapshot:
    category: str           # "saves" or "extdata"
    title_dir: str          # exact folder name on disk
    name: str               # exact snapshot folder name
    timestamp: datetime     # parsed from name (naive, console-local)
    label: str              # text after the timestamp, may be empty
    path: Path
    size_bytes: int | None = None   # None until measured
    file_count: int | None = None

    @property
    def key(self) -> str:
        """Card-relative key, used for protection and selection."""
        return f"{self.category}/{self.title_dir}/{self.name}"

    @property
    def measured(self) -> bool:
        return self.size_bytes is not None


@dataclass
class Title:
    category: str
    dir_name: str
    title_id: str | None
    display_name: str
    path: Path
    snapshots: list[Snapshot] = field(default_factory=list)  # newest first

    @property
    def key(self) -> str:
        return f"{self.category}/{self.dir_name}"

    @property
    def total_size(self) -> int:
        return sum(s.size_bytes or 0 for s in self.snapshots)

    @property
    def fully_measured(self) -> bool:
        return all(s.measured for s in self.snapshots)

    @property
    def newest(self) -> datetime | None:
        return self.snapshots[0].timestamp if self.snapshots else None


@dataclass(frozen=True)
class Skipped:
    path: Path
    reason: str


@dataclass
class CheckpointRoot:
    path: Path
    platform: str                     # "3ds", "switch" or "unknown"
    categories: list[str]             # which of saves/extdata exist
    titles: list[Title]
    skipped: list[Skipped]

    def all_snapshots(self) -> list[Snapshot]:
        return [s for t in self.titles for s in t.snapshots]

    def title_by_key(self, key: str) -> Title | None:
        for t in self.titles:
            if t.key == key:
                return t
        return None

    def update_snapshot(self, snap: Snapshot) -> None:
        """Swap in a measured copy of a snapshot."""
        for t in self.titles:
            if t.category == snap.category and t.dir_name == snap.title_dir:
                for i, s in enumerate(t.snapshots):
                    if s.name == snap.name:
                        t.snapshots[i] = snap
                        return


# Parsing

def parse_title_dir(name: str) -> tuple[str | None, str]:
    """Split a title folder name into (title_id, display_name)."""
    m = TITLE_ID_RE.match(name)
    if m:
        return m.group(1), m.group(2)
    return None, name


def parse_snapshot_name(name: str) -> tuple[datetime, str] | None:
    """Return (timestamp, label) or None if the name is not a snapshot."""
    m = SNAPSHOT_RE.match(name)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None  # e.g. 20251399-999999
    return ts, m.group(2)


def snapshot_sort_key(s: Snapshot):
    return (s.timestamp, s.label, s.name)


# Link and junction detection

def is_link_or_junction(path: Path) -> bool:
    """True for symlinks, Windows junctions and other reparse points."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    isjunction = getattr(os.path, "isjunction", None)  # Python 3.12+
    if isjunction is not None and isjunction(path):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse)


# Root discovery

def detect_platform(root: Path) -> str:
    parent = root.parent.name.casefold()
    if parent in ("3ds", "switch"):
        return parent
    return "unknown"


def is_checkpoint_root(path: Path) -> bool:
    """Spec 5.1: saves/ or extdata/, plus config.json or titles.sha."""
    try:
        has_cat = any((path / c).is_dir() for c in CATEGORIES)
        has_marker = (path / "config.json").is_file() or (path / "titles.sha").is_file()
    except OSError:
        return False
    return has_cat and has_marker


def resolve_root(path: str | os.PathLike, platform: str = "auto") -> Path:
    """Turn a user-supplied path into a validated Checkpoint folder.

    Accepts the Checkpoint folder itself or an SD root. Raises RootError,
    or AmbiguousRootError when an SD root holds both layouts and the
    platform was left on auto.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise RootError(f"Path not found: {p}")
    if not p.is_dir():
        raise RootError(f"Not a folder: {p}")
    if is_checkpoint_root(p):
        return p

    candidates: dict[str, Path] = {}
    for plat in ("3ds", "switch"):
        c = p / plat / "Checkpoint"
        if is_checkpoint_root(c):
            candidates[plat] = c

    if platform in ("3ds", "switch"):
        if platform in candidates:
            return candidates[platform]
        raise RootError(f"No {platform}/Checkpoint folder found under {p}")
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    if len(candidates) > 1:
        raise AmbiguousRootError(
            f"{p} holds both 3ds/Checkpoint and switch/Checkpoint. "
            "Pass --platform 3ds or --platform switch, or point at the Checkpoint folder.",
            candidates,
        )
    raise RootError(
        f"{p} is not a Checkpoint folder. Expected saves/ or extdata/ plus config.json "
        "or titles.sha, either here or under 3ds/Checkpoint or switch/Checkpoint."
    )


# Scanning

def _scandir(path: Path) -> list[os.DirEntry]:
    with os.scandir(path) as it:
        return sorted(it, key=lambda e: e.name)


def load_root(root: Path) -> CheckpointRoot:
    """Names-only scan of saves/ and extdata/. Fast; sizes come later."""
    root = Path(root)
    if not is_checkpoint_root(root):
        raise RootError(f"{root} is not a Checkpoint folder (or it is no longer mounted)")

    titles: list[Title] = []
    skipped: list[Skipped] = []
    categories: list[str] = []

    for cat in CATEGORIES:
        cat_path = root / cat
        if not cat_path.is_dir():
            continue
        if is_link_or_junction(cat_path):
            skipped.append(Skipped(cat_path, "category folder is a link, not followed"))
            continue
        categories.append(cat)
        try:
            entries = _scandir(cat_path)
        except OSError as e:
            skipped.append(Skipped(cat_path, f"cannot read: {e.strerror or e}"))
            continue

        for entry in entries:
            tpath = Path(entry.path)
            if is_link_or_junction(tpath):
                skipped.append(Skipped(tpath, "link, not followed"))
                continue
            if not entry.is_dir(follow_symlinks=False):
                skipped.append(Skipped(tpath, "file where a title folder was expected"))
                continue
            tid, tname = parse_title_dir(entry.name)
            title = Title(cat, entry.name, tid, tname, tpath)
            try:
                snap_entries = _scandir(tpath)
            except OSError as e:
                skipped.append(Skipped(tpath, f"cannot read: {e.strerror or e}"))
                titles.append(title)
                continue
            for se in snap_entries:
                spath = Path(se.path)
                if is_link_or_junction(spath):
                    skipped.append(Skipped(spath, "link, not followed"))
                    continue
                if not se.is_dir(follow_symlinks=False):
                    skipped.append(Skipped(spath, "file inside a title folder"))
                    continue
                parsed = parse_snapshot_name(se.name)
                if parsed is None:
                    skipped.append(Skipped(spath, "name does not start with YYYYMMDD-HHMMSS"))
                    continue
                ts, label = parsed
                title.snapshots.append(Snapshot(cat, entry.name, se.name, ts, label, spath))
            title.snapshots.sort(key=snapshot_sort_key, reverse=True)
            titles.append(title)

    return CheckpointRoot(root, detect_platform(root), categories, titles, skipped)


def measure_path(path: Path) -> tuple[int, int, list[str]]:
    """Total bytes and file count under a folder. Never follows links."""
    total = 0
    count = 0
    errors: list[str] = []
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink() or is_link_or_junction(Path(entry.path)):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError as e:
                        errors.append(f"{entry.path}: {e.strerror or e}")
        except OSError as e:
            errors.append(f"{current}: {e.strerror or e}")
    return total, count, errors


def measure(snap: Snapshot) -> Snapshot:
    """Return a copy of the snapshot with size and file count filled in."""
    size, count, _ = measure_path(snap.path)
    return replace(snap, size_bytes=size, file_count=count)


def measure_all(root: CheckpointRoot, snaps: list[Snapshot] | None = None) -> None:
    """Measure snapshots in place on the model (blocking; CLI use)."""
    for s in snaps if snaps is not None else root.all_snapshots():
        if not s.measured:
            root.update_snapshot(measure(s))


def format_size(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
