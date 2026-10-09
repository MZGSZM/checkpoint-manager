# Checkpoint Manager

Checkpoint Manager lists and prunes the backups that the Checkpoint homebrew app leaves on a 3DS (or Switch) SD card, for anyone whose card is slowly filling up with timestamped save folders that nothing ever cleans up.

Checkpoint makes a new folder every time you back up a title and never deletes an old one. My card had 578 snapshots and 1.2 GB of them, and 96% of that was extdata. This tool runs on a PC with the card mounted, shows you what is there, and deletes old snapshots either by hand in a terminal UI or by rule from the command line.

It only reads and deletes. It never creates, renames, restores or edits a backup, and it never touches anything outside `saves/` and `extdata/` (plus `logs/` if you ask it to).

## Prerequisites

- Python 3.10 or newer
- Textual (pulls in `rich` with it)
- The SD card mounted on the PC, in a card reader or whichever way you normally get at it
- pytest, only if you want to run the tests

## Setup

1. Grab the folder and `cd` into it: `cd checkpoint-manager`
2. Install the one dependency: `pip install -r requirements.txt`
   I'm on openSUSE, where the system Python is externally managed, so in my case it's `python3 -m venv .venv && . .venv/bin/activate` first and then the pip line. On Windows it's just `py -m pip install -r requirements.txt`. It depends on your system.
3. Find where your card is mounted. Mine shows up at `/run/media/$USER/3DS`. On Windows it'll be a drive letter like `E:\`, on macOS something like `/Volumes/3DS`. You'll have to check.
4. Run it: `python main.py /run/media/$USER/3DS`

You can point it at the SD root or straight at the Checkpoint folder (`.../3ds/Checkpoint`), whichever. It remembers the last one, so after the first run plain `python main.py` is enough.

If the card has both `3ds/Checkpoint` and `switch/Checkpoint`, the TUI asks which one, and the CLI wants `--platform 3ds` or `--platform switch`.

## The TUI

`python main.py` with no subcommand opens it. Left pane is titles, right pane is the snapshots for the highlighted title, newest first. Sizes fill in a second or two after it opens, since they're measured in the background so a slow SD reader never freezes the screen.

The fast path for "keep the newest 4 of this title": highlight the title, press `o`, `Enter`, `d`, then `Tab` and `Enter` on Delete.

| Key | Does |
| --- | --- |
| `Up` `Down` / `j` `k` | Move |
| `Tab` | Switch pane |
| `Space` | Toggle a snapshot, or every snapshot of a title on the left |
| `a` / `n` | Select all / none in the current pane |
| `o` | Select all but the newest N of the current title (asks, default 4) |
| `p` | Prune dialog: keep newest N, older than D, keep at least N, with a live preview |
| `P` | Protect or un-protect the highlighted snapshot |
| `d` / `Delete` | Delete the selection (always asks first) |
| `s` | Cycle sort: size, name, count, newest |
| `/` | Filter titles by name or ID |
| `r` | Rescan |
| `?` | Help |
| `q` | Quit |

Flags in the snapshot list: `P` is protected, `!` is possibly incomplete (no files, or under a quarter of the title's median size, which is what a cancelled backup looks like).

The Prune dialog's default button is Select. It marks what the rules would remove so you can look it over and adjust by hand before pressing `d`. Delete is one button over if you trust the preview.

The delete dialog starts with Cancel focused. Anything over 25 snapshots or 500 MB makes you type `delete` before the button unlocks.

## The CLI

Every destructive command is a dry run unless you add `--apply`.

```
python main.py list   [ROOT] [filters] [--json | --tsv]
python main.py stats  [ROOT] [filters] [--json]
python main.py prune  [ROOT] --keep-newest N | --older-than D [filters] [--apply] [--yes]
python main.py delete [ROOT] --title X --snapshot NAME [--snapshot NAME ...] [--apply] [--yes]
python main.py logs   [ROOT] --older-than 30d [--apply] [--yes]
```

Filters: `--category saves|extdata|both`, `--title PATTERN` and `--exclude-title PATTERN` (both repeatable; a hex ID like `0x00308` or a case-insensitive glob like `'pok*'`).

Some examples from my card:

```
# What is eating the card?
python main.py stats

# Preview keeping the four newest of everything
python main.py prune --keep-newest 4

# Do it for Minecraft only, no prompt
python main.py prune --title 0x01B87 --keep-newest 4 --apply --yes

# Anything older than six months, but never below two per title
python main.py prune --older-than 6m --keep-min 2 --apply

# Move to a folder on the PC instead of deleting
python main.py prune --keep-newest 4 --trash-dir ~/ckpt-trash --apply

# Pipe it somewhere
python main.py list --json | jq '.snapshots[] | select(.possibly_incomplete)'
```

`--older-than` takes `90d`, `12w`, `6m`, `1y` or an ISO date like `2025-12-01`. If you give both rules, a snapshot goes if either one says so, and then `--keep-min` (default 1) saves the newest N of each title no matter what.

Retention counts saves and extdata separately, so Mario Kart 7's saves and its extdata each keep their own four. `--combine-categories` counts them together if you really want that.

With `--json`, stdout is one JSON document and every human message goes to stderr, so it pipes cleanly.

Exit codes: `0` fine (dry runs too), `1` some deletions failed, `2` usage error or bad root, `3` refused by a safety check, `130` interrupted.

## Safety

Deleted saves are gone. The SD card has no recycle bin.

- **It only deletes snapshot folders.** A folder has to be `<root>/saves/<title>/<YYYYMMDD-HHMMSS...>` or the extdata equivalent, not a symlink or junction, and resolve inside the root. That check runs again right before each folder is removed, not just at scan time. Title folders, `config.json`, the cache files, `scripts/` and the extra folders listed in `config.json` can't be deleted by any code path.
- **Anything it doesn't recognise is left alone.** Folders that don't start with a valid timestamp show up as skipped and are never offered for deletion.
- **Rules never empty a title by default.** `--keep-min 0` is allowed, but `--keep-min 0 --yes` is refused unless you also pass `--allow-delete-all`.
- **Protected snapshots are off limits.** Rules skip them, and deleting one by hand means un-protecting it first.
- **Scripts need `--yes`.** If stdin isn't a terminal, `--apply` without `--yes` stops with exit 2 instead of guessing.
- **One failure doesn't stop the run.** Locked files, a read-only card or a pulled card are collected and reported at the end with counts and bytes reclaimed. Read-only file attributes are cleared and retried once.
- **The trash folder has to be on the PC.** `--trash-dir` refuses a folder on the same drive as the card, since moving within the card frees nothing.

If you get at the card over the network instead of a reader, close Checkpoint on the console first. Two things writing to one card at once ends badly.

AND EJECT THE CARD PROPERLY BEFORE YOU PULL IT. Deleting a few hundred folders means a lot of pending FAT writes, and yanking the card mid-flush can corrupt the whole filesystem, live saves included.

## Local state

Settings live on the PC, never on the card:

- Linux: `~/.config/checkpoint-manager/state.json`
- macOS: `~/Library/Application Support/checkpoint-manager/state.json`
- Windows: `%APPDATA%\checkpoint-manager\state.json`

It holds the last root, the TUI sort order, the default N for `o`, the trash folder and the protected list. Protected entries are keyed by card-relative path (`extdata/0x0008F Theme/20250520-213146`), so they survive the card coming up under a different drive letter. If a protected snapshot is gone from a card that still has that title, the entry is dropped; entries for titles not on the current card are left alone, so a second card doesn't wipe the first card's list. Delete the file to reset everything. A corrupt one is ignored.

The TUI remembers a trash folder: `python main.py tui --trash-dir ~/ckpt-trash` sets it, `--clear-trash-dir` forgets it. The CLI only moves to trash when you pass `--trash-dir` on that command, so a script never changes behaviour behind your back.

## Switch

Switch support is a best guess. It's built from the Checkpoint README and synthetic test folders, and has not been tried on a real Switch SD card.

What it assumes: `switch/Checkpoint/saves/<title id> <game title>/<YYYYMMDD-HHMMSS...>`, a 16-digit hex ID with or without `0x`, and no extdata. If the real layout differs, the failure mode is safe: folders that don't parse are listed as skipped and nothing gets deleted. If you have a Switch and try it, I'd like to hear how it goes.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest tests
```

That covers the rules, the parser quirks (no ID, double spaces, `…`, `®`, `é`, labels after the timestamp), the deletion whitelist, CLI exit codes and a headless TUI run at 80x24.

To poke at a realistic card without risking a real one, rebuild one from a Checkpoint report JSON:

```
python tests/fixtures.py report /tmp/fakecard Checkpoint_report.json --real-sizes
python main.py /tmp/fakecard
```

`--real-sizes` makes sparse files with the reported sizes, so stats and prune numbers match the real card. `small` and `switch` instead of `report` build the smaller test trees.

## Notes

- Back-to-back pairs (two snapshots seconds apart, which Checkpoint seems to make for some extdata) are treated as two separate backups. Keep-4 means four folders, even if two of them are the same backup twice.
- A title where every snapshot is empty, like NSMB2 or Pokemon Bank extdata, isn't flagged as incomplete. That's just what those games store.
- Ages come from the folder name, never the file times, since FAT32 mtimes are coarse and change when the card is copied around. The time is whatever the console clock said.

Now your card shows only the backups you meant to keep, everything else is either gone or sitting in a trash folder on the PC, and Checkpoint will rebuild its caches the next time it starts. Tinker with the rules as you see fit.
