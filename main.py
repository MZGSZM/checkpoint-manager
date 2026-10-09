#!/usr/bin/env python3
"""Checkpoint Manager: browse and prune Checkpoint save backups on a 3DS or Switch SD card.

Run with no subcommand (or `tui`) for the interactive interface, or use
list / stats / prune / delete / logs for scripting.
"""

from __future__ import annotations

import argparse
import sys

__version__ = "0.1.0"

COMMANDS = ("tui", "list", "stats", "prune", "delete", "logs")


def _nonneg_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("root", nargs="?", help="Checkpoint folder or SD card root (default: last used)")
    common.add_argument("--root", dest="root_opt", metavar="PATH", help="same as the positional ROOT")
    common.add_argument("--platform", choices=("auto", "3ds", "switch"), default="auto",
                        help="pick a layout when an SD root holds both (default: auto)")
    common.add_argument("--no-color", action="store_true", help="plain output (NO_COLOR is honoured too)")
    common.add_argument("-q", "--quiet", action="store_true", help="errors only")

    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument("--category", choices=("saves", "extdata", "both"), default="both")
    filters.add_argument("--title", action="append", metavar="PATTERN",
                         help="hex ID (0x00308) or case-insensitive name glob; repeatable")
    filters.add_argument("--exclude-title", action="append", metavar="PATTERN", help="repeatable")

    jsonflag = argparse.ArgumentParser(add_help=False)
    jsonflag.add_argument("--json", action="store_true", help="machine-readable output on stdout")

    applying = argparse.ArgumentParser(add_help=False)
    applying.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    applying.add_argument("-y", "--yes", action="store_true",
                          help="skip the confirmation prompt; required when stdin is not a terminal")

    p = argparse.ArgumentParser(
        prog="main.py",
        description="Browse and prune Checkpoint backups on a mounted 3DS or Switch SD card.",
        epilog="With no subcommand the TUI starts: python main.py [ROOT]",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    t = sub.add_parser("tui", parents=[common], help="interactive interface (default)")
    t.add_argument("--trash-dir", metavar="PATH",
                   help="offer Move instead of Delete, into this folder on the PC (remembered)")
    t.add_argument("--clear-trash-dir", action="store_true", help="forget the remembered trash folder")

    l = sub.add_parser("list", parents=[common, filters, jsonflag], help="one row per snapshot")
    l.add_argument("--tsv", action="store_true", help="tab-separated output")

    sub.add_parser("stats", parents=[common, filters, jsonflag], help="per-title counts and sizes, largest first")

    pr = sub.add_parser("prune", parents=[common, filters, jsonflag, applying], help="delete by retention rule")
    pr.add_argument("--keep-newest", type=_nonneg_int, metavar="N", help="keep the N newest per title")
    pr.add_argument("--older-than", metavar="D", help="delete older than 90d, 12w, 6m, 1y or an ISO date")
    pr.add_argument("--keep-min", type=_nonneg_int, default=1, metavar="N",
                    help="always keep at least N newest per title (default 1)")
    pr.add_argument("--combine-categories", action="store_true",
                    help="count a title's saves and extdata together")
    prot = pr.add_mutually_exclusive_group()
    prot.add_argument("--respect-protected", dest="ignore_protected", action="store_false", default=False,
                      help="never delete protected snapshots (default)")
    prot.add_argument("--ignore-protected", dest="ignore_protected", action="store_true",
                      help="let rules delete protected snapshots too")
    pr.add_argument("--allow-delete-all", action="store_true",
                    help="needed with --keep-min 0 --yes")
    pr.add_argument("--trash-dir", metavar="PATH", help="move to this folder on the PC instead of deleting")
    pr.add_argument("-v", "--verbose", action="store_true", help="list every snapshot in the plan")

    d = sub.add_parser("delete", parents=[common, filters, jsonflag, applying], help="delete named snapshots")
    d.add_argument("--snapshot", action="append", required=True, metavar="NAME",
                   help="snapshot folder name; repeatable")
    d.add_argument("--trash-dir", metavar="PATH", help="move to this folder on the PC instead of deleting")

    lg = sub.add_parser("logs", parents=[common, jsonflag, applying], help="delete old Checkpoint log files")
    lg.add_argument("--older-than", required=True, metavar="D", help="for example 30d")
    return p


def normalise_argv(argv: list[str]) -> list[str]:
    """No subcommand means the TUI, so `main.py /path` works."""
    if not argv:
        return ["tui"]
    if argv[0] in COMMANDS or argv[0] in ("-h", "--help", "--version"):
        return argv
    return ["tui", *argv]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    args = parser.parse_args(normalise_argv(sys.argv[1:] if argv is None else argv))

    from cli import EXIT_INTERRUPTED, CliExit

    try:
        if args.command == "tui":
            from tui import run_tui
            return run_tui(args)

        import cli
        if args.command == "delete" and not args.title:
            raise CliExit(2, "delete needs --title.")
        handler = {"list": cli.cmd_list, "stats": cli.cmd_stats, "prune": cli.cmd_prune,
                   "delete": cli.cmd_delete, "logs": cli.cmd_logs}[args.command]
        return handler(args)
    except CliExit as e:
        if e.message:
            print(f"error: {e.message}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
