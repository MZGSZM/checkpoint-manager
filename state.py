"""Local state on the PC (spec 9). Never written to the SD card.

Corrupt or missing state is not an error: everything falls back to defaults.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "checkpoint-manager"
SORT_ORDERS = ("size", "name", "count", "newest")


def config_dir() -> Path:
    override = os.environ.get("CHECKPOINT_MANAGER_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


@dataclass
class State:
    last_root: str | None = None
    sort: str = "size"
    default_keep: int = 4
    trash_dir: str | None = None
    protected: list[str] = field(default_factory=list)

    @property
    def protected_set(self) -> set[str]:
        return set(self.protected)

    def set_protected(self, key: str, on: bool) -> None:
        keys = self.protected_set
        if on:
            keys.add(key)
        else:
            keys.discard(key)
        self.protected = sorted(keys)

    def drop_missing(self, existing_keys: set[str], title_keys: set[str] | None = None) -> bool:
        """Drop protected entries whose snapshot is gone. Returns True if changed.

        With title_keys, only entries for titles present on this card are
        judged, so opening a second card never wipes the first card's list.
        """
        def keep(k: str) -> bool:
            if k in existing_keys:
                return True
            if title_keys is None:
                return False
            return k.rsplit("/", 1)[0] not in title_keys
        kept = [k for k in self.protected if keep(k)]
        changed = len(kept) != len(self.protected)
        self.protected = kept
        return changed


def state_path() -> Path:
    return config_dir() / "state.json"


def load_state() -> State:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return State()
    except (OSError, ValueError):
        return State()
    st = State()
    if isinstance(raw.get("last_root"), str):
        st.last_root = raw["last_root"]
    if raw.get("sort") in SORT_ORDERS:
        st.sort = raw["sort"]
    if isinstance(raw.get("default_keep"), int) and raw["default_keep"] >= 0:
        st.default_keep = raw["default_keep"]
    if isinstance(raw.get("trash_dir"), str):
        st.trash_dir = raw["trash_dir"]
    if isinstance(raw.get("protected"), list):
        st.protected = sorted({k for k in raw["protected"] if isinstance(k, str)})
    return st


def save_state(st: State) -> None:
    """Atomic write. Failure to save is reported to the caller, never fatal."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(st), f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
