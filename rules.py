"""Retention rules and filters. Pure functions: no I/O, no filesystem access.

Shared by the TUI Prune dialog and the CLI prune command (spec 6).
"""

from __future__ import annotations

import calendar
import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from scan import Snapshot, Title, snapshot_sort_key

AGE_RE = re.compile(r"^\s*(\d+)\s*([dwmy])\s*$", re.IGNORECASE)


def subtract_months(dt: datetime, months: int) -> datetime:
    total = dt.year * 12 + (dt.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def parse_age(value: str, now: datetime | None = None) -> datetime:
    """Turn '90d', '12w', '6m', '1y' or an ISO date into a cutoff datetime.

    Snapshots strictly older than the cutoff count as old.
    """
    now = now or datetime.now()
    m = AGE_RE.match(value)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit == "d":
            return now - timedelta(days=n)
        if unit == "w":
            return now - timedelta(weeks=n)
        if unit == "m":
            return subtract_months(now, n)
        return subtract_months(now, 12 * n)
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(
            f"Cannot read age {value!r}. Use 90d, 12w, 6m, 1y or an ISO date like 2025-12-01."
        ) from None


@dataclass
class PruneParams:
    keep_newest: int | None = None
    older_than: datetime | None = None   # cutoff, already parsed
    keep_min: int = 1
    combine_categories: bool = False
    respect_protected: bool = True

    def validate(self) -> None:
        if self.keep_newest is None and self.older_than is None:
            raise ValueError("Give at least one rule: keep newest N or older than D.")
        if self.keep_newest is not None and self.keep_newest < 0:
            raise ValueError("keep newest must be 0 or more.")
        if self.keep_min < 0:
            raise ValueError("keep min must be 0 or more.")


@dataclass
class Plan:
    delete: list[Snapshot] = field(default_factory=list)
    keep: list[Snapshot] = field(default_factory=list)
    protected_skipped: list[Snapshot] = field(default_factory=list)

    @property
    def delete_bytes(self) -> int:
        return sum(s.size_bytes or 0 for s in self.delete)

    @property
    def unmeasured_deletes(self) -> int:
        return sum(1 for s in self.delete if not s.measured)

    @property
    def keep_bytes(self) -> int:
        return sum(s.size_bytes or 0 for s in self.keep + self.protected_skipped)


def _group_key(title: Title, combine: bool) -> tuple:
    if combine:
        return ("*", (title.title_id or title.dir_name).casefold())
    return (title.category, title.dir_name)


def plan_prune(titles: list[Title], params: PruneParams,
               protected: set[str] | frozenset[str] = frozenset()) -> Plan:
    """Spec 6.2: mark by keep_newest OR older_than, unmark keep_min newest,
    then pull protected snapshots out of the delete set."""
    params.validate()
    groups: dict[tuple, list[Snapshot]] = {}
    for t in titles:
        groups.setdefault(_group_key(t, params.combine_categories), []).extend(t.snapshots)

    plan = Plan()
    for snaps in groups.values():
        ordered = sorted(snaps, key=snapshot_sort_key, reverse=True)
        marked = [False] * len(ordered)
        for i, s in enumerate(ordered):
            if params.keep_newest is not None and i >= params.keep_newest:
                marked[i] = True
            if params.older_than is not None and s.timestamp < params.older_than:
                marked[i] = True
        for i in range(min(params.keep_min, len(ordered))):
            marked[i] = False
        for s, m in zip(ordered, marked):
            if not m:
                plan.keep.append(s)
            elif params.respect_protected and s.key in protected:
                plan.protected_skipped.append(s)
            else:
                plan.delete.append(s)
    return plan


def all_but_newest(title: Title, n: int, protected: set[str] | frozenset[str] = frozenset()) -> list[Snapshot]:
    """TUI 'o' key: everything except the newest n, protected ones left out."""
    ordered = sorted(title.snapshots, key=snapshot_sort_key, reverse=True)
    return [s for s in ordered[max(n, 0):] if s.key not in protected]


# Filters (spec 6.4, 10.1)

def title_matches(title: Title, pattern: str) -> bool:
    """Hex ID or case-insensitive glob against the name or folder name."""
    p = pattern.casefold()
    candidates = [title.display_name.casefold(), title.dir_name.casefold()]
    if title.title_id:
        candidates.append(title.title_id.casefold())
    return any(fnmatch.fnmatchcase(c, p) for c in candidates)


def filter_titles(titles: list[Title], category: str = "both",
                  include: list[str] | None = None,
                  exclude: list[str] | None = None) -> list[Title]:
    out = []
    for t in titles:
        if category != "both" and t.category != category:
            continue
        if include and not any(title_matches(t, p) for p in include):
            continue
        if exclude and any(title_matches(t, p) for p in exclude):
            continue
        out.append(t)
    return out


# Possibly incomplete snapshots (spec 11, cancelled backups)

INCOMPLETE_RATIO = 0.25


def incomplete_keys(title: Title) -> set[str]:
    """Flag snapshots with no files, or far smaller than the title's median.

    Only measured snapshots are judged. A title where every snapshot is
    empty is how some games' extdata looks (NSMB2, Pokemon Bank), so those
    are not flagged: there is no normal-sized neighbour to compare against.
    """
    measured = [s for s in title.snapshots if s.measured]
    if not measured:
        return set()
    med_size = median(s.size_bytes for s in measured)
    med_count = median(s.file_count for s in measured)
    if med_size == 0 and med_count == 0:
        return set()
    flagged = set()
    for s in measured:
        if s.file_count == 0 or s.size_bytes < med_size * INCOMPLETE_RATIO:
            flagged.add(s.key)
    return flagged
