"""Build synthetic Checkpoint trees for tests and manual poking.

    python tests/fixtures.py small  DEST          # quirk-heavy 3DS tree
    python tests/fixtures.py switch DEST          # switch/Checkpoint tree
    python tests/fixtures.py report DEST REPORT.json [--real-sizes]

The report builder rebuilds a card from a Checkpoint_report.json tree,
expanding collapsed "series" back into individual folders. Files are tiny
placeholders unless --real-sizes is given, in which case they are sparse
files with the reported sizes (cheap on Linux and macOS, not on FAT).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_JSON = {
    "additional_extdata_folders": {}, "additional_save_folders": {},
    "favorites": [], "filter": [], "nand_saves": False, "scan_cart": False, "version": 3,
}


def _write(path: Path, size: int, real_sizes: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        if real_sizes:
            f.truncate(size)
        else:
            f.write(b"x" * min(size, 16))


def _markers(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(CONFIG_JSON, indent=4))
    (root / "titles.sha").write_bytes(b"\0" * 32)
    (root / "fullsavecache").write_bytes(b"cache")
    (root / "fullextdatacache").write_bytes(b"cache")


# From a report

def _build_node(node: dict, dest: Path, real_sizes: bool) -> None:
    names = [node["name"]]
    if "series" in node:
        names += [m["name"] for m in node["series"]["members"] if m["name"] != node["name"]]
    for name in names:
        target = dest / name
        if node["type"] == "file":
            _write(target, node.get("size", 0), real_sizes)
        else:
            target.mkdir(parents=True, exist_ok=True)
            for child in node.get("children", []):
                _build_node(child, target, real_sizes)


def build_from_report(report: Path, dest: Path, real_sizes: bool = False) -> Path:
    data = json.loads(Path(report).read_text(encoding="utf-8"))
    root = Path(dest) / "3ds" / "Checkpoint"
    root.mkdir(parents=True, exist_ok=True)
    for child in data["tree"]["children"]:
        _build_node(child, root, real_sizes)
    return root


# Small quirk-heavy 3DS tree

THEME_TIMES = ["20250520-213146", "20250520-213152", "20250526-214245", "20250526-214251",
               "20250602-003049", "20250602-003056", "20250608-225806", "20250608-225813",
               "20250616-233657", "20250616-233704", "20250622-221141", "20250622-221148",
               "20250630-005709", "20250630-005717", "20250706-234048", "20250706-234056",
               "20250720-222857", "20250720-222905", "20250813-215223", "20250813-215230",
               "20251206-155202", "20251206-155208", "20261008-145347", "20261008-145355"]

WEEKLY = ["20250520-212712", "20250526-213759", "20250602-002535", "20250608-225251",
          "20250616-233139", "20250622-220613", "20250630-005137", "20250706-233510",
          "20250720-222308", "20250813-214638", "20251206-154845", "20261008-144727"]


def build_small(dest: Path) -> Path:
    """24-snapshot Theme extdata, MK7 in both categories, and every parser quirk."""
    root = Path(dest) / "3ds" / "Checkpoint"
    _markers(root)
    ext = root / "extdata"
    sv = root / "saves"

    for t in THEME_TIMES:
        for f, size in (("Cache.dat", 5768), ("CacheD.dat", 5000), ("SaveData.dat", 1168)):
            _write(ext / "0x0008F Theme" / t / f, size, True)
    for t in WEEKLY:
        _write(ext / "0x00308 MARIO KART 7" / t / "data.dat", 1200, True)
        _write(sv / "0x00308 MARIO KART 7" / t / "system1.dat", 200, True)
        _write(sv / "0x00308 MARIO KART 7" / t / "replay" / "replay04.dat", 100, True)
        (ext / "0x007AE New Super Mario Bros  2" / t).mkdir(parents=True, exist_ok=True)

    # Possibly incomplete: an empty and a tiny snapshot among normal ones.
    for t in WEEKLY[:6]:
        _write(sv / "0x011C4 Pokémon Omega Ruby" / t / "main", 4000, True)
    (sv / "0x011C4 Pokémon Omega Ruby" / WEEKLY[6]).mkdir(parents=True)
    _write(sv / "0x011C4 Pokémon Omega Ruby" / WEEKLY[7] / "main", 10, True)

    # Names: ®, …, double spaces, label after the timestamp, no ID, odd ID.
    _write(sv / "0x012EC Tetris® Ultimate" / "20250520-212628" / "save.dat", 969, True)
    _write(sv / "0x01D14 Mario & Luigi  Bowser's Inside…" / "20250608-225041" / "ML3R_001.sav", 500, True)
    _write(sv / "0x019BD Mario Party  Star Rush" / "20250720-221700" / "mf1", 512, True)
    _write(sv / "0x019BD Mario Party  Star Rush" / "20250813-214417 before boss" / "mf1", 512, True)
    _write(sv / "0xF700E Super Mario World" / "20261008-144647" / "KTR-UAAE.cfg", 164, True)
    (sv / "DSPI HOMEBREW").mkdir(parents=True)
    (sv / "0x0137E NSMB 2  Gold Edition").mkdir(parents=True)
    (ext / "0x0137E NSMB 2  Gold Edition").mkdir(parents=True)

    # Things that must never be offered for deletion.
    (sv / "0x00308 MARIO KART 7" / "notes").mkdir()
    (sv / "0x00308 MARIO KART 7" / "20251399-999999").mkdir()   # invalid date
    (sv / "0x00308 MARIO KART 7" / "readme.txt").write_text("not a snapshot")
    (sv / "stray.bin").write_bytes(b"x")
    logs = root / "logs"
    logs.mkdir()
    for day in ("20250427", "20250520", "20261008"):
        (logs / f"checkpoint_{day}.log").write_text("log\n")
    (logs / "other.txt").write_text("keep me")
    (root / "scripts" / "universal").mkdir(parents=True)
    (root / "scripts" / "universal" / "script.lua").write_text("-- user script")
    return root


def build_switch(dest: Path) -> Path:
    """switch/Checkpoint with saves only (spec 13, Switch fixtures)."""
    root = Path(dest) / "switch" / "Checkpoint"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(CONFIG_JSON))
    sv = root / "saves"
    for title in ("0x0100000000010000 Super Mario Odyssey",
                  "01006F8002326000 Animal Crossing",
                  "FACE Off", "ABBA Gold"):
        for t in WEEKLY[:6]:
            _write(sv / title / t / "save.bin", 300, True)
    _write(sv / "FACE Off" / "20261008-144727 Player1" / "save.bin", 300, True)
    (sv / "01006F8002326000 Animal Crossing" / "Player1").mkdir()   # unrecognised
    (root / "logs").mkdir()
    (root / "scripts").mkdir()
    return root


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    kind, dest = sys.argv[1], Path(sys.argv[2])
    if kind == "small":
        print(build_small(dest))
    elif kind == "switch":
        print(build_switch(dest))
    elif kind == "report" and len(sys.argv) >= 4:
        print(build_from_report(Path(sys.argv[3]), dest, "--real-sizes" in sys.argv))
    else:
        print(__doc__)
        sys.exit(2)
