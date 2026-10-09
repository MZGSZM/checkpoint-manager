"""Non-interactive commands: list, stats, prune, delete, logs (spec 10)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

import delete as deleter
import rules
import scan
from scan import CheckpointRoot, Snapshot, Title, format_size
from state import State, load_state, save_state

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2
EXIT_SAFETY = 3
EXIT_INTERRUPTED = 130

BIG_COUNT = 25
BIG_BYTES = 500 * 1024 * 1024


class CliExit(Exception):
    def __init__(self, code: int, message: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Out:
    """stdout for data, stderr for messages when --json is on (spec 10.4)."""
    json_mode: bool
    quiet: bool
    no_color: bool

    def __post_init__(self):
        no_color = self.no_color or bool(os.environ.get("NO_COLOR"))
        self.data = Console(no_color=no_color, highlight=False, soft_wrap=False)
        self.msg_console = Console(stderr=True, no_color=no_color, highlight=False)

    def info(self, text: str) -> None:
        if not self.quiet:
            (self.msg_console if self.json_mode else self.data).print(text, markup=False)

    def warn(self, text: str) -> None:
        if not self.quiet:
            self.msg_console.print(text, markup=False)

    def error(self, text: str) -> None:
        self.msg_console.print(text, markup=False)

    def emit_json(self, obj) -> None:
        sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def make_out(args) -> Out:
    return Out(getattr(args, "json", False), getattr(args, "quiet", False), getattr(args, "no_color", False))


# Root handling

def pick_root_arg(args, state: State) -> str:
    given = getattr(args, "root_opt", None) or getattr(args, "root", None)
    if given:
        return given
    if state.last_root:
        return state.last_root
    raise CliExit(EXIT_USAGE, "No root given and none remembered. Pass the Checkpoint folder or SD root.")


def open_root(args, state: State, out: Out) -> CheckpointRoot:
    path = pick_root_arg(args, state)
    try:
        resolved = scan.resolve_root(path, getattr(args, "platform", "auto"))
        root = scan.load_root(resolved)
    except scan.RootError as e:
        raise CliExit(EXIT_USAGE, str(e)) from None
    state.last_root = str(resolved)
    state.drop_missing({s.key for s in root.all_snapshots()}, {t.key for t in root.titles})
    try:
        save_state(state)
    except OSError as e:
        out.warn(f"Note: could not save state ({e.strerror or e}).")
    return root


def current(root: CheckpointRoot, snaps: list[Snapshot]) -> list[Snapshot]:
    """Look up the latest (measured) copies of snapshots on the model."""
    out = []
    for s in snaps:
        t = root.title_by_key(f"{s.category}/{s.title_dir}")
        out.append(next((x for x in t.snapshots if x.name == s.name), s) if t else s)
    return out


def scoped_titles(root: CheckpointRoot, args, out: Out) -> list[Title] | None:
    """Apply --category/--title/--exclude-title. None means 'nothing to do'."""
    cat = getattr(args, "category", "both")
    if cat != "both" and cat not in root.categories:
        out.warn(f"This root has no {cat}/ folder (platform: {root.platform}). Nothing to do.")
        return None
    return rules.filter_titles(root.titles, cat, getattr(args, "title", None), getattr(args, "exclude_title", None))


# Serialisation

def snap_dict(s: Snapshot, title: Title | None, protected: set[str], incomplete: set[str]) -> dict:
    return {
        "category": s.category,
        "title_id": title.title_id if title else None,
        "title": title.display_name if title else s.title_dir,
        "title_dir": s.title_dir,
        "snapshot": s.name,
        "timestamp": s.timestamp.isoformat(),
        "label": s.label,
        "size_bytes": s.size_bytes,
        "file_count": s.file_count,
        "protected": s.key in protected,
        "possibly_incomplete": s.key in incomplete,
        "path": str(s.path),
    }


def new_table(root: CheckpointRoot, columns: list[tuple[str, dict]]) -> tuple[Table, bool]:
    """Every column keeps its natural width except Title, which gives way."""
    table = Table(show_edge=False, pad_edge=False, header_style="bold", box=None, padding=(0, 1))
    show_cat = len(root.categories) > 1
    if show_cat:
        table.add_column("Cat", no_wrap=True, min_width=3)
    for name, kw in columns:
        opts = {"no_wrap": True}
        if name == "Title":
            opts.update(overflow="ellipsis", ratio=1, min_width=12)
        else:
            opts["min_width"] = max(len(name), kw.pop("width", 0))
        opts.update(kw)
        table.add_column(name, **opts)
    return table, show_cat


def title_index(root: CheckpointRoot) -> dict[tuple[str, str], Title]:
    return {(t.category, t.dir_name): t for t in root.titles}


def trunc(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def skipped_list(root: CheckpointRoot) -> list[dict]:
    return [{"path": str(s.path), "reason": s.reason} for s in root.skipped]


def report_skipped(root: CheckpointRoot, out: Out) -> None:
    if root.skipped and not out.json_mode:
        out.warn(f"Skipped / unrecognised ({len(root.skipped)}), never offered for deletion:")
        for s in root.skipped:
            out.warn(f"  {s.path}  ({s.reason})")


# list

def cmd_list(args) -> int:
    out = make_out(args)
    state = load_state()
    root = open_root(args, state, out)
    titles = scoped_titles(root, args, out)
    if titles is None:
        if out.json_mode:
            out.emit_json({"root": str(root.path), "platform": root.platform, "snapshots": [], "skipped": skipped_list(root)})
        return EXIT_OK
    snaps = [s for t in titles for s in t.snapshots]
    scan.measure_all(root, snaps)
    protected = state.protected_set
    rows = []
    for t in titles:
        inc = rules.incomplete_keys(t)
        for s in t.snapshots:
            rows.append(snap_dict(s, t, protected, inc))

    if out.json_mode:
        out.emit_json({"root": str(root.path), "platform": root.platform, "snapshots": rows, "skipped": skipped_list(root)})
        return EXIT_OK
    if args.tsv:
        cols = ["category", "title_id", "title", "snapshot", "timestamp", "label",
                "size_bytes", "file_count", "protected", "possibly_incomplete"]
        sys.stdout.write("\t".join(cols) + "\n")
        for r in rows:
            vals = ["" if r[c] is None else str(r[c]).lower() if isinstance(r[c], bool) else str(r[c]) for c in cols]
            sys.stdout.write("\t".join(v.replace("\t", " ") for v in vals) + "\n")
        report_skipped(root, out)
        return EXIT_OK

    table, show_cat = new_table(root, [
        ("ID", {"width": 7}), ("Title", {}), ("Snapshot", {"width": 15}),
        ("Size", {"justify": "right", "width": 8}), ("Files", {"justify": "right"}), ("Flags", {})])
    for r in rows:
        flags = ("P" if r["protected"] else "") + ("!" if r["possibly_incomplete"] else "")
        cells = [r["title_id"] or "-", r["title"], r["snapshot"],
                 format_size(r["size_bytes"]), str(r["file_count"]), flags]
        if show_cat:
            cells.insert(0, "S" if r["category"] == "saves" else "E")
        table.add_row(*cells)
    out.data.print(table)
    total = sum(r["size_bytes"] or 0 for r in rows)
    out.info(f"{len(rows)} snapshots, {format_size(total)}. Flags: P protected, ! possibly incomplete.")
    report_skipped(root, out)
    return EXIT_OK


# stats

def cmd_stats(args) -> int:
    out = make_out(args)
    state = load_state()
    root = open_root(args, state, out)
    titles = scoped_titles(root, args, out)
    if titles is None:
        if out.json_mode:
            out.emit_json({"root": str(root.path), "platform": root.platform, "titles": [], "totals": {"titles": 0, "snapshots": 0, "size_bytes": 0}})
        return EXIT_OK
    scan.measure_all(root, [s for t in titles for s in t.snapshots])
    ordered = sorted(titles, key=lambda t: (-t.total_size, t.dir_name.casefold()))
    by_cat: dict[str, dict] = {}
    for t in titles:
        c = by_cat.setdefault(t.category, {"titles": 0, "snapshots": 0, "size_bytes": 0})
        c["titles"] += 1
        c["snapshots"] += len(t.snapshots)
        c["size_bytes"] += t.total_size
    totals = {"titles": len(titles), "snapshots": sum(len(t.snapshots) for t in titles),
              "size_bytes": sum(t.total_size for t in titles)}

    if out.json_mode:
        out.emit_json({
            "root": str(root.path), "platform": root.platform,
            "titles": [{
                "category": t.category, "title_id": t.title_id, "title": t.display_name,
                "title_dir": t.dir_name, "snapshots": len(t.snapshots), "size_bytes": t.total_size,
                "newest": t.newest.isoformat() if t.newest else None,
                "oldest": t.snapshots[-1].timestamp.isoformat() if t.snapshots else None,
            } for t in ordered],
            "by_category": by_cat, "totals": totals,
            "skipped": skipped_list(root),
        })
        return EXIT_OK

    table, show_cat = new_table(root, [
        ("ID", {"width": 7}), ("Title", {}), ("Snaps", {"justify": "right"}),
        ("Size", {"justify": "right", "width": 8}), ("Newest", {"width": 10})])
    for t in ordered:
        cells = [t.title_id or "-", t.display_name, str(len(t.snapshots)),
                 format_size(t.total_size), t.newest.strftime("%Y-%m-%d") if t.newest else "-"]
        if show_cat:
            cells.insert(0, "S" if t.category == "saves" else "E")
        table.add_row(*cells)
    out.data.print(table)
    for cat, c in by_cat.items():
        out.info(f"{cat}: {c['titles']} titles, {c['snapshots']} snapshots, {format_size(c['size_bytes'])}")
    out.info(f"Total: {totals['snapshots']} snapshots, {format_size(totals['size_bytes'])}")
    report_skipped(root, out)
    return EXIT_OK


# Shared apply flow for prune and delete

def confirm(count: int, size: int, out: Out, trash: Path | None) -> bool:
    verb = f"move {count} snapshot(s) to {trash}" if trash else f"PERMANENTLY delete {count} snapshot(s)"
    out.warn(f"About to {verb} ({format_size(size)}).")
    try:
        if count > BIG_COUNT or size > BIG_BYTES:
            answer = input("Type 'delete' to proceed: ")
            return answer.strip() == "delete"
        answer = input("Proceed? [y/N] ")
        return answer.strip().lower() in ("y", "yes")
    except EOFError:
        return False


def apply_deletions(args, root: CheckpointRoot, snaps: list[Snapshot], out: Out) -> tuple[int, deleter.RunResult | None]:
    if not snaps:
        out.info("Nothing to delete.")
        return EXIT_OK, None
    if not args.yes and not sys.stdin.isatty():
        raise CliExit(EXIT_USAGE, "stdin is not a terminal: pass --yes to apply without a prompt.")
    trash = Path(args.trash_dir).expanduser() if getattr(args, "trash_dir", None) else None
    if trash is not None:
        try:
            deleter.verify_trash_dir(root.path, trash)
        except deleter.SafetyError as e:
            raise CliExit(EXIT_SAFETY, str(e)) from None
    size = sum(s.size_bytes or 0 for s in snaps)
    if not args.yes and not confirm(len(snaps), size, out, trash):
        out.warn("Cancelled. Nothing was changed.")
        return EXIT_OK, None

    def progress(i, total, snap):
        if snap is not None and not out.quiet and not out.json_mode and sys.stderr.isatty():
            sys.stderr.write(f"\r[{i + 1}/{total}] {trunc(snap.key, 60):<60}")
            sys.stderr.flush()
        elif snap is None and sys.stderr.isatty() and not out.quiet and not out.json_mode:
            sys.stderr.write("\r" + " " * 72 + "\r")

    result = deleter.delete_snapshots(root.path, snaps, trash, progress)
    verb = "Moved" if trash else "Deleted"
    out.info(f"{verb} {len(result.deleted)}, failed {len(result.failed)}, reclaimed {format_size(result.bytes_reclaimed)}.")
    for f in result.failed:
        out.error(f"  FAILED {f.key}: {f.error}")
    out.info("Eject the card safely before pulling it.")
    return (EXIT_FAILURES if result.failed else EXIT_OK), result


def result_json(result: deleter.RunResult | None) -> dict:
    if result is None:
        return {"deleted": [], "failed": [], "bytes_reclaimed": 0}
    return {"deleted": result.deleted,
            "failed": [{"key": f.key, "error": f.error} for f in result.failed],
            "bytes_reclaimed": result.bytes_reclaimed,
            "moved_to": result.moved_to}


# prune

def print_plan(plan: rules.Plan, root: CheckpointRoot, out: Out, verbose: bool) -> None:
    groups: dict[tuple[str, str], dict] = {}
    for kind, snaps in (("delete", plan.delete), ("keep", plan.keep), ("protected", plan.protected_skipped)):
        for s in snaps:
            g = groups.setdefault((s.category, s.title_dir), {"delete": [], "keep": [], "protected": []})
            g[kind].append(s)
    idx = title_index(root)
    table, show_cat = new_table(root, [
        ("ID", {"width": 7}), ("Title", {}), ("Delete", {"justify": "right"}),
        ("Keep", {"justify": "right"}), ("Reclaim", {"justify": "right", "width": 8}),
        ("Kept", {"justify": "right", "width": 8})])
    ordered = sorted(groups.items(), key=lambda kv: -sum(s.size_bytes or 0 for s in kv[1]["delete"]))
    for (cat, tdir), g in ordered:
        if not g["delete"] and not verbose:
            continue
        t = idx.get((cat, tdir))
        keep_n = len(g["keep"]) + len(g["protected"])
        cells = [t.title_id if t and t.title_id else "-", t.display_name if t else tdir,
                 str(len(g["delete"])), f"{keep_n}" + (f" ({len(g['protected'])}P)" if g["protected"] else ""),
                 format_size(sum(s.size_bytes or 0 for s in g["delete"])),
                 format_size(sum(s.size_bytes or 0 for s in g["keep"] + g["protected"]))]
        if show_cat:
            cells.insert(0, "S" if cat == "saves" else "E")
        table.add_row(*cells)
    if table.row_count:
        out.data.print(table)
    if verbose:
        for s in sorted(plan.delete, key=lambda s: (s.category, s.title_dir.casefold(), s.name)):
            out.info(f"  delete  {s.key}  {format_size(s.size_bytes)}")
        for s in plan.protected_skipped:
            out.info(f"  protected, kept  {s.key}")
    out.info(f"Plan: delete {len(plan.delete)} ({format_size(plan.delete_bytes)}), "
             f"keep {len(plan.keep) + len(plan.protected_skipped)} ({format_size(plan.keep_bytes)}).")


def plan_json(plan: rules.Plan, root: CheckpointRoot, protected: set[str]) -> dict:
    idx = title_index(root)
    conv = lambda snaps: [snap_dict(s, idx.get((s.category, s.title_dir)), protected, set()) for s in snaps]
    return {"delete": conv(plan.delete), "keep": conv(plan.keep),
            "protected_skipped": conv(plan.protected_skipped),
            "delete_count": len(plan.delete), "delete_bytes": plan.delete_bytes,
            "keep_count": len(plan.keep) + len(plan.protected_skipped), "keep_bytes": plan.keep_bytes}


def cmd_prune(args) -> int:
    out = make_out(args)
    if args.keep_newest is None and args.older_than is None:
        raise CliExit(EXIT_USAGE, "prune needs at least one of --keep-newest or --older-than.")
    try:
        cutoff = rules.parse_age(args.older_than) if args.older_than else None
    except ValueError as e:
        raise CliExit(EXIT_USAGE, str(e)) from None
    params = rules.PruneParams(args.keep_newest, cutoff, args.keep_min,
                               args.combine_categories, not args.ignore_protected)
    try:
        params.validate()
    except ValueError as e:
        raise CliExit(EXIT_USAGE, str(e)) from None
    if args.apply and args.keep_min == 0 and args.yes and not args.allow_delete_all:
        raise CliExit(EXIT_SAFETY, "Refused: --keep-min 0 with --yes can delete every snapshot of a title. "
                                   "Add --allow-delete-all if you really mean it.")

    state = load_state()
    root = open_root(args, state, out)
    titles = scoped_titles(root, args, out)
    if titles is None:
        if out.json_mode:
            out.emit_json({"plan": None, "dry_run": not args.apply, **result_json(None)})
        return EXIT_OK
    scan.measure_all(root, [s for t in titles for s in t.snapshots])
    protected = state.protected_set
    plan = rules.plan_prune(titles, params, protected)

    if not out.json_mode:
        print_plan(plan, root, out, args.verbose)
    if args.keep_min == 0:
        out.warn("Warning: --keep-min 0 means a title can lose every snapshot it has.")

    code, result = EXIT_OK, None
    if args.apply:
        code, result = apply_deletions(args, root, plan.delete, out)
    elif not out.json_mode:
        out.info("Dry run. Nothing was changed. Add --apply to do it.")
    report_skipped(root, out)
    if out.json_mode:
        out.emit_json({"plan": plan_json(plan, root, protected), "dry_run": not args.apply, **result_json(result)})
    return code


# delete

def cmd_delete(args) -> int:
    out = make_out(args)
    state = load_state()
    root = open_root(args, state, out)
    titles = scoped_titles(root, args, out)
    if titles is None:
        if out.json_mode:
            out.emit_json({"plan": None, "dry_run": not args.apply, **result_json(None)})
        return EXIT_OK
    if not titles:
        raise CliExit(EXIT_USAGE, f"No title matches {', '.join(args.title)}.")

    chosen: list[Snapshot] = []
    for name in dict.fromkeys(args.snapshot):
        hits = [s for t in titles for s in t.snapshots if s.name == name]
        if not hits:
            raise CliExit(EXIT_USAGE, f"Snapshot {name!r} not found in the matching title(s).")
        if len(hits) > 1:
            where = ", ".join(s.key for s in hits)
            raise CliExit(EXIT_USAGE, f"Snapshot {name!r} matches more than one title ({where}). "
                                      "Narrow it with --category or a more specific --title.")
        chosen.append(hits[0])

    protected = state.protected_set
    locked = [s for s in chosen if s.key in protected]
    if locked:
        raise CliExit(EXIT_SAFETY, "Refused: protected snapshot(s): " + ", ".join(s.key for s in locked)
                      + ". Un-protect them in the TUI (P) first.")

    scan.measure_all(root, chosen)
    chosen = current(root, chosen)

    idx = title_index(root)
    if not out.json_mode:
        for s in chosen:
            out.info(f"  delete  {s.key}  {format_size(s.size_bytes)}  {s.file_count} files")
        out.info(f"{len(chosen)} snapshot(s), {format_size(sum(s.size_bytes or 0 for s in chosen))}.")

    code, result = EXIT_OK, None
    if args.apply:
        code, result = apply_deletions(args, root, chosen, out)
    elif not out.json_mode:
        out.info("Dry run. Nothing was changed. Add --apply to do it.")
    if out.json_mode:
        plan = {"delete": [snap_dict(s, idx.get((s.category, s.title_dir)), protected, set()) for s in chosen],
                "delete_count": len(chosen), "delete_bytes": sum(s.size_bytes or 0 for s in chosen)}
        out.emit_json({"plan": plan, "dry_run": not args.apply, **result_json(result)})
    return code


# logs

def find_old_logs(root: Path, cutoff: datetime) -> list[Path]:
    logs = Path(root) / "logs"
    if not logs.is_dir() or scan.is_link_or_junction(logs):
        return []
    found = []
    for p in sorted(logs.iterdir()):
        if not deleter.LOG_RE.match(p.name) or not p.is_file() or scan.is_link_or_junction(p):
            continue
        try:
            day = datetime.strptime(p.name[11:19], "%Y%m%d")
        except ValueError:
            continue
        if day < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
            found.append(p)
    return found


def cmd_logs(args) -> int:
    out = make_out(args)
    try:
        cutoff = rules.parse_age(args.older_than)
    except ValueError as e:
        raise CliExit(EXIT_USAGE, str(e)) from None
    state = load_state()
    root = open_root(args, state, out)
    old = find_old_logs(root.path, cutoff)
    size = sum(p.stat().st_size for p in old)
    if not out.json_mode:
        for p in old:
            out.info(f"  delete  logs/{p.name}  {format_size(p.stat().st_size)}")
        out.info(f"{len(old)} log file(s) older than {cutoff:%Y-%m-%d}, {format_size(size)}.")

    result = None
    code = EXIT_OK
    if args.apply and old:
        if not args.yes and not sys.stdin.isatty():
            raise CliExit(EXIT_USAGE, "stdin is not a terminal: pass --yes to apply without a prompt.")
        ok = args.yes
        if not ok:
            try:
                ok = input(f"Delete {len(old)} log file(s)? [y/N] ").strip().lower() in ("y", "yes")
            except EOFError:
                ok = False
        if ok:
            result = deleter.delete_logs(root.path, old)
            out.info(f"Deleted {len(result.deleted)}, failed {len(result.failed)}, reclaimed {format_size(result.bytes_reclaimed)}.")
            for f in result.failed:
                out.error(f"  FAILED {f.key}: {f.error}")
            code = EXIT_FAILURES if result.failed else EXIT_OK
        else:
            out.warn("Cancelled. Nothing was changed.")
    elif not args.apply and not out.json_mode:
        out.info("Dry run. Nothing was changed. Add --apply to do it.")
    if out.json_mode:
        out.emit_json({"plan": {"delete": [f"logs/{p.name}" for p in old], "delete_bytes": size},
                       "dry_run": not args.apply, **result_json(result)})
    return code
