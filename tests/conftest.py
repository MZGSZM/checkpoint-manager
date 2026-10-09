import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import fixtures  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Each test gets its own state.json, never the real one."""
    monkeypatch.setenv("CHECKPOINT_MANAGER_HOME", str(tmp_path / "state-home"))
    monkeypatch.delenv("NO_COLOR", raising=False)


@pytest.fixture
def small(tmp_path):
    return fixtures.build_small(tmp_path / "card")


@pytest.fixture
def switch(tmp_path):
    return fixtures.build_switch(tmp_path / "swcard")


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}
