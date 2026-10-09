from datetime import datetime
from pathlib import Path

import pytest

import rules
from scan import Snapshot, Title

NOW = datetime(2026, 10, 9, 12, 0, 0)


def make_title(stamps, category="saves", name="0x00001 Test", sizes=None, labels=None):
    snaps = []
    for i, st in enumerate(stamps):
        label = labels[i] if labels else ""
        ts = datetime.strptime(st, "%Y%m%d-%H%M%S")
        size = sizes[i] if sizes else None
        snaps.append(Snapshot(category, name, st + label, ts, label, Path("/x") / (st + label),
                              size, None if size is None else (0 if size == 0 else 1)))
    snaps.sort(key=lambda s: (s.timestamp, s.label, s.name), reverse=True)
    tid, disp = name.split(" ", 1) if name.startswith("0x") else (None, name)
    return Title(category, name, tid, disp, Path("/x"), snaps)


MONTHLY = [f"2026{m:02d}01-120000" for m in range(1, 11)]   # Jan..Oct 2026


def names(snaps):
    return sorted(s.name for s in snaps)


@pytest.mark.parametrize("params, expected_deleted", [
    (dict(keep_newest=4), MONTHLY[:6]),
    (dict(keep_newest=10), []),
    (dict(keep_newest=20), []),                                    # fewer than N
    (dict(keep_newest=0), MONTHLY[:9]),                            # keep_min 1 saves newest
    (dict(keep_newest=0, keep_min=0), MONTHLY),
    (dict(older_than=datetime(2026, 4, 15)), MONTHLY[:4]),
    (dict(older_than=datetime(2027, 1, 1)), MONTHLY[:9]),          # keep_min wins
    (dict(older_than=datetime(2027, 1, 1), keep_min=3), MONTHLY[:7]),
    (dict(keep_newest=8, older_than=datetime(2026, 4, 15)), MONTHLY[:4]),   # either marks
    (dict(keep_newest=2, older_than=datetime(2026, 1, 15)), MONTHLY[:8]),
])
def test_rules_table(params, expected_deleted):
    title = make_title(MONTHLY)
    plan = rules.plan_prune([title], rules.PruneParams(**params))
    assert names(plan.delete) == sorted(expected_deleted)
    assert len(plan.delete) + len(plan.keep) == len(MONTHLY)


def test_needs_a_rule():
    with pytest.raises(ValueError):
        rules.plan_prune([make_title(MONTHLY)], rules.PruneParams())


def test_empty_title_nothing_to_do():
    plan = rules.plan_prune([make_title([])], rules.PruneParams(keep_newest=1))
    assert plan.delete == [] and plan.keep == []


def test_equal_timestamps_ordered_by_label():
    t = make_title(["20260101-120000", "20260101-120000", "20260101-120000"],
                   labels=["", " a", " b"])
    plan = rules.plan_prune([t], rules.PruneParams(keep_newest=1))
    assert [s.label for s in plan.keep] == [" b"]
    assert sorted(s.label for s in plan.delete) == ["", " a"]


def test_categories_counted_separately():
    a = make_title(MONTHLY[:6], "saves", "0x00308 MARIO KART 7")
    b = make_title(MONTHLY[4:], "extdata", "0x00308 MARIO KART 7")
    plan = rules.plan_prune([a, b], rules.PruneParams(keep_newest=4))
    assert len(plan.delete) == 2 + 2
    combined = rules.plan_prune([a, b], rules.PruneParams(keep_newest=4, combine_categories=True))
    assert len(combined.delete) == 12 - 4


def test_protected_never_deleted_by_rules():
    t = make_title(MONTHLY)
    oldest = next(s for s in t.snapshots if s.name == MONTHLY[0])
    plan = rules.plan_prune([t], rules.PruneParams(keep_newest=1), {oldest.key})
    assert oldest not in plan.delete
    assert oldest in plan.protected_skipped
    ignored = rules.plan_prune([t], rules.PruneParams(keep_newest=1, respect_protected=False), {oldest.key})
    assert oldest in ignored.delete


def test_worked_example_theme():
    """Spec 6.3: Theme extdata, 24 snapshots in back-to-back pairs."""
    import fixtures
    t = make_title(fixtures.THEME_TIMES, "extdata", "0x0008F Theme")
    plan = rules.plan_prune([t], rules.PruneParams(keep_newest=4))
    assert len(plan.delete) == 20 and len(plan.keep) == 4
    assert names(plan.keep) == sorted(fixtures.THEME_TIMES[-4:])
    six = rules.parse_age("6m", NOW)
    plan = rules.plan_prune([t], rules.PruneParams(keep_newest=4, older_than=six))
    # The 2025-12-06 pair is inside the newest 4 but older than six months
    assert len(plan.delete) == 22
    assert names(plan.keep) == ["20261008-145347", "20261008-145355"]
    plan = rules.plan_prune([t], rules.PruneParams(older_than=rules.parse_age("1d", datetime(2030, 1, 1))))
    assert [s.name for s in plan.keep] == ["20261008-145355"]   # keep_min 1


@pytest.mark.parametrize("value, expected", [
    ("90d", datetime(2026, 7, 11, 12, 0)),
    ("12w", datetime(2026, 7, 17, 12, 0)),
    ("6m", datetime(2026, 4, 9, 12, 0)),
    ("1y", datetime(2025, 10, 9, 12, 0)),
    ("2025-12-01", datetime(2025, 12, 1)),
    (" 3M ", datetime(2026, 7, 9, 12, 0)),
])
def test_parse_age(value, expected):
    assert rules.parse_age(value, NOW) == expected


def test_parse_age_month_end():
    assert rules.parse_age("1m", datetime(2026, 3, 31)) == datetime(2026, 2, 28)


@pytest.mark.parametrize("bad", ["", "ten days", "5x", "-3d", "2026-13-01"])
def test_parse_age_rejects(bad):
    with pytest.raises(ValueError):
        rules.parse_age(bad, NOW)


def test_all_but_newest():
    t = make_title(MONTHLY)
    picks = rules.all_but_newest(t, 4)
    assert names(picks) == sorted(MONTHLY[:6])
    prot = {picks[0].key}
    assert len(rules.all_but_newest(t, 4, prot)) == 5
    assert rules.all_but_newest(t, 50) == []


def test_filters():
    a = make_title(MONTHLY, "saves", "0x01B87 Minecraft")
    b = make_title(MONTHLY, "extdata", "0x01B87 Minecraft")
    c = make_title(MONTHLY, "saves", "0x00C9B Pokémon Bank")
    d = make_title(MONTHLY, "saves", "DSPI HOMEBREW")
    all_ = [a, b, c, d]
    assert rules.filter_titles(all_, "extdata") == [b]
    assert rules.filter_titles(all_, include=["0x01b87"]) == [a, b]
    assert rules.filter_titles(all_, include=["MINECRAFT"]) == [a, b]
    assert rules.filter_titles(all_, include=["pok*"]) == [c]
    assert rules.filter_titles(all_, include=["dspi*"]) == [d]
    assert rules.filter_titles(all_, exclude=["0x01B87"]) == [c, d]
    assert rules.filter_titles(all_, "saves", include=["*"], exclude=["*bank"]) == [a, d]


def test_incomplete_flags():
    t = make_title(MONTHLY[:6], sizes=[4000, 4000, 0, 10, 4000, 4000])
    flagged = {s.name for s in t.snapshots if s.key in rules.incomplete_keys(t)}
    assert flagged == {MONTHLY[2], MONTHLY[3]}
    # Every snapshot empty is normal for some extdata: nothing flagged
    empty = make_title(MONTHLY[:4], sizes=[0, 0, 0, 0])
    assert rules.incomplete_keys(empty) == set()
    # Unmeasured snapshots are not judged
    assert rules.incomplete_keys(make_title(MONTHLY)) == set()
