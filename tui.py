"""Textual interface (spec 7)."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, ProgressBar, Select, Static
from textual.worker import get_current_worker

import delete as deleter
import rules
import scan
from scan import CheckpointRoot, Snapshot, Title, format_size
from state import SORT_ORDERS, State, load_state, save_state

BIG_COUNT = 25
BIG_BYTES = 500 * 1024 * 1024


def trunc(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "\u2026"


# Modal dialogs

class PathPrompt(ModalScreen[str | None]):
    """Ask for a Checkpoint folder or SD root."""

    BINDINGS = [Binding("escape", "cancel", "Quit")]

    def __init__(self, message: str = "", default: str = ""):
        super().__init__()
        self.message = message
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Checkpoint folder or SD card root:", classes="title")
            if self.message:
                yield Static(self.message, classes="error")
            yield Input(value=self.default, placeholder="/run/media/me/3DS  or  E:\\3ds\\Checkpoint", id="path")
            with Horizontal(classes="buttons"):
                yield Button("Open", variant="primary", id="ok")
                yield Button("Quit", id="quit")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def submit(self) -> None:
        value = self.query_one("#path", Input).value.strip()
        if value:
            self.dismiss(value)

    @on(Button.Pressed, "#quit")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoosePlatform(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("This SD card has both 3ds/Checkpoint and switch/Checkpoint.", classes="title")
            yield Static("Which one do you want to open?")
            with Horizontal(classes="buttons"):
                yield Button("3DS", variant="primary", id="3ds")
                yield Button("Switch", id="switch")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AskNumber(ModalScreen[int | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, default: int):
        super().__init__()
        self.prompt = prompt
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog small"):
            yield Label(self.prompt, classes="title")
            yield Input(value=str(self.default), id="n", type="integer")
            yield Static("", id="err", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def submit(self) -> None:
        try:
            n = int(self.query_one("#n", Input).value)
            if n < 0:
                raise ValueError
        except ValueError:
            self.query_one("#err", Static).update("Enter a whole number, 0 or more.")
            return
        self.dismiss(n)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_TEXT = """\
[b]Moving[/b]
  Up/Down, j/k   move          Tab   switch pane
  /              filter titles  s     cycle sort (size, name, count, newest)

[b]Selecting[/b]
  Space   toggle a snapshot, or every snapshot of a title
  a / n   select all / none in the current pane
  o       select all but the newest N of the current title
  p       prune dialog (keep newest N, older than D)
  P       protect / un-protect the highlighted snapshot

[b]Acting[/b]
  d, Delete   delete the selection (always confirms)
  r           rescan the card
  q           quit

[b]Flags[/b]
  P  protected: rules never touch it, un-protect before deleting by hand
  !  possibly incomplete: no files, or far smaller than its neighbours

Deleting is permanent. The SD card has no recycle bin.
Start with --trash-dir PATH to move snapshots to a folder on the PC instead.
Eject the card safely when you are done."""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("Checkpoint Manager keys", classes="title")
            yield Static(HELP_TEXT)
            with Horizontal(classes="buttons"):
                yield Button("Close", variant="primary", id="close")

    def on_button_pressed(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class ErrorScreen(ModalScreen[str]):
    """Card gone or root unusable: rescan or quit (spec 7.5)."""

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Problem with the card", classes="title")
            yield Static(self.message, classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Rescan", variant="primary", id="rescan")
                yield Button("Open another", id="other")
                yield Button("Quit", id="quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


class PruneDialog(ModalScreen[tuple[str, list[Snapshot]] | None]):
    """Rules from spec 6 with a live preview. Default action is Select."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, root: CheckpointRoot, current: Title | None, protected: set[str], default_keep: int):
        super().__init__()
        self.root = root
        self.current = current
        self.protected = protected
        self.default_keep = default_keep
        self.plan: rules.Plan | None = None

    def compose(self) -> ComposeResult:
        scopes = [("Whole card", "card")]
        if self.current is not None:
            scopes.append((f"This title: {trunc(self.current.display_name, 28)}", "title"))
        with Vertical(classes="dialog wide"):
            yield Label("Prune by rule", classes="title")
            with Horizontal(classes="row"):
                yield Label("Keep newest N", classes="field")
                yield Input(value=str(self.default_keep), id="keep", type="integer", compact=True)
            with Horizontal(classes="row"):
                yield Label("Older than", classes="field")
                yield Input(placeholder="90d, 12w, 6m, 1y or 2025-12-01 (blank = off)", id="older", compact=True)
            with Horizontal(classes="row"):
                yield Label("Keep at least", classes="field")
                yield Input(value="1", id="keepmin", type="integer", compact=True)
            with Horizontal(classes="row"):
                yield Label("Scope", classes="field")
                yield Select(scopes, value="card", allow_blank=False, id="scope", compact=True)
            if len(self.root.categories) > 1:
                with Horizontal(classes="row"):
                    yield Label("Category", classes="field")
                    yield Select([("Saves and extdata", "both"), ("Saves only", "saves"),
                                  ("Extdata only", "extdata")], value="both", allow_blank=False, id="category",
                                 compact=True)
            yield Static("", id="preview")
            with Horizontal(classes="buttons"):
                yield Button("Select", variant="primary", id="select")
                yield Button("Delete...", variant="error", id="delete")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.update_preview()
        self.query_one("#select", Button).focus()

    @on(Input.Changed)
    @on(Select.Changed)
    def update_preview(self) -> None:
        preview = self.query_one("#preview", Static)
        try:
            keep_raw = self.query_one("#keep", Input).value.strip()
            older_raw = self.query_one("#older", Input).value.strip()
            keep_min_raw = self.query_one("#keepmin", Input).value.strip() or "1"
            params = rules.PruneParams(
                keep_newest=int(keep_raw) if keep_raw else None,
                older_than=rules.parse_age(older_raw) if older_raw else None,
                keep_min=int(keep_min_raw),
            )
            params.validate()
        except ValueError as e:
            self.plan = None
            preview.update(Text(str(e) or "Check the numbers.", style="red"))
            self._buttons(False)
            return
        scope = self.query_one("#scope", Select).value
        titles = [self.current] if scope == "title" and self.current else self.root.titles
        if len(self.root.categories) > 1:
            titles = rules.filter_titles(titles, self.query_one("#category", Select).value)
        self.plan = rules.plan_prune(titles, params, self.protected)
        p = self.plan
        unsized = f" (+{p.unmeasured_deletes} not sized yet)" if p.unmeasured_deletes else ""
        lines = [f"Delete {len(p.delete)} snapshot(s), reclaim {format_size(p.delete_bytes)}{unsized}",
                 f"Keep {len(p.keep) + len(p.protected_skipped)}"
                 + (f" ({len(p.protected_skipped)} protected)" if p.protected_skipped else "")]
        if params.keep_min == 0:
            lines.append(Text("Keep at least 0: a title can lose every snapshot.", style="bold red").plain)
        preview.update("\n".join(lines))
        self._buttons(bool(p.delete))

    def _buttons(self, enabled: bool) -> None:
        self.query_one("#select", Button).disabled = not enabled
        self.query_one("#delete", Button).disabled = not enabled

    @on(Button.Pressed, "#select")
    def do_select(self) -> None:
        if self.plan:
            self.dismiss(("select", list(self.plan.delete)))

    @on(Button.Pressed, "#delete")
    def do_delete(self) -> None:
        if self.plan:
            self.dismiss(("delete", list(self.plan.delete)))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDelete(ModalScreen[str | None]):
    """Spec 7.4. Default focus is Cancel; big runs need 'delete' typed."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, snaps: list[Snapshot], titles: dict[str, Title], trash: Path | None):
        super().__init__()
        self.snaps = snaps
        self.titles = titles
        self.trash = trash
        self.total_bytes = sum(s.size_bytes or 0 for s in snaps)
        self.unsized = sum(1 for s in snaps if not s.measured)
        self.big = len(snaps) > BIG_COUNT or self.total_bytes > BIG_BYTES

    def compose(self) -> ComposeResult:
        affected: dict[str, int] = {}
        for s in self.snaps:
            affected[f"{s.category}/{s.title_dir}"] = affected.get(f"{s.category}/{s.title_dir}", 0) + 1
        lines = []
        for key, n in sorted(affected.items(), key=lambda kv: -kv[1])[:8]:
            t = self.titles.get(key)
            tag = "S" if key.startswith("saves/") else "E"
            lines.append(f"  {tag} {trunc(t.display_name if t else key, 40)}: {n}")
        if len(affected) > 8:
            lines.append(f"  ...and {len(affected) - 8} more title(s)")
        size = format_size(self.total_bytes) + (f" (+{self.unsized} not sized yet)" if self.unsized else "")
        with Vertical(classes="dialog wide"):
            yield Label(f"Delete {len(self.snaps)} snapshot(s) from {len(affected)} title(s)?", classes="title")
            yield Static(f"Total size: {size}\n" + "\n".join(lines))
            if self.trash:
                yield Static(f"Move puts them in {self.trash} on the PC. Delete is permanent.")
            else:
                yield Static(Text("This is permanent. The SD card has no recycle bin.", style="bold red"))
            if self.big:
                yield Static("This is a big one. Type delete to enable the buttons:")
                yield Input(placeholder="delete", id="typed")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", variant="primary", id="cancel")
                if self.trash:
                    yield Button("Move to trash", variant="warning", id="move", disabled=self.big)
                yield Button("Delete", variant="error", id="delete", disabled=self.big)

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Input.Changed, "#typed")
    def typed(self, event: Input.Changed) -> None:
        ok = event.value.strip() == "delete"
        for b in self.query("#move, #delete"):
            b.disabled = not ok

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProgressScreen(ModalScreen[deleter.RunResult | None]):
    """Runs the deletion in a worker thread, then shows the summary."""

    def __init__(self, root: Path, snaps: list[Snapshot], trash: Path | None):
        super().__init__()
        self.root_path = root
        self.snaps = snaps
        self.trash = trash
        self.result: deleter.RunResult | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("Moving to trash..." if self.trash else "Deleting...", classes="title", id="heading")
            yield ProgressBar(total=len(self.snaps), show_eta=False, id="bar")
            yield Static("", id="current")
            with Horizontal(classes="buttons"):
                yield Button("Close", variant="primary", id="close", disabled=True)

    def on_mount(self) -> None:
        self.run_delete()

    @work(thread=True, exclusive=True, group="delete")
    def run_delete(self) -> None:
        def progress(i, total, snap):
            self.app.call_from_thread(self._progress, i, snap)
        result = deleter.delete_snapshots(self.root_path, self.snaps, self.trash, progress)
        self.app.call_from_thread(self._done, result)

    def _progress(self, i: int, snap: Snapshot | None) -> None:
        self.query_one("#bar", ProgressBar).update(progress=i)
        self.query_one("#current", Static).update(trunc(snap.key, 70) if snap else "")

    def _done(self, result: deleter.RunResult) -> None:
        self.result = result
        verb = "Moved" if self.trash else "Deleted"
        self.query_one("#heading", Label).update("Finished")
        lines = [f"{verb} {len(result.deleted)}, failed {len(result.failed)}, "
                 f"reclaimed {format_size(result.bytes_reclaimed)}."]
        for f in result.failed[:10]:
            lines.append(f"  FAILED {trunc(f.key, 40)}: {trunc(f.error, 60)}")
        if len(result.failed) > 10:
            lines.append(f"  ...and {len(result.failed) - 10} more")
        lines.append("Eject the card safely before pulling it.")
        self.query_one("#current", Static).update("\n".join(lines))
        close = self.query_one("#close", Button)
        close.disabled = False
        close.focus()

    @on(Button.Pressed, "#close")
    def close(self) -> None:
        self.dismiss(self.result)


# Main screen

class MainScreen(Screen):
    BINDINGS = [
        Binding("space", "toggle", "Select"),
        Binding("a", "select_all", "All"),
        Binding("n", "select_none", "None"),
        Binding("o", "all_but_newest", "All but N"),
        Binding("p", "prune", "Prune"),
        Binding("P", "protect", "Protect"),
        Binding("d", "delete", "Delete"),
        Binding("delete", "delete", "Delete", show=False),
        Binding("s", "sort", "Sort"),
        Binding("slash", "filter", "Filter"),
        Binding("r", "rescan", "Rescan"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "app.quit", "Quit"),
        Binding("j", "cursor('down')", "Down", show=False),
        Binding("k", "cursor('up')", "Up", show=False),
        Binding("escape", "close_filter", "Close filter", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Filter titles (Enter or Esc to close)", id="filter")
        with Horizontal(id="panes"):
            yield DataTable(id="titles", cursor_type="row", zebra_stripes=True, cell_padding=0)
            yield DataTable(id="snapshots", cursor_type="row", zebra_stripes=True, cell_padding=0)
        yield Static("", id="status")
        yield Footer()

    @property
    def app_(self) -> "CheckpointApp":
        return self.app  # type: ignore[return-value]

    def on_mount(self) -> None:
        self.query_one("#filter", Input).display = False
        self.query_one("#titles", DataTable).focus()

    # Delegated actions

    def action_toggle(self) -> None:
        self.app_.toggle_current(self.focused_pane())

    def action_select_all(self) -> None:
        self.app_.select_all(self.focused_pane(), True)

    def action_select_none(self) -> None:
        self.app_.select_all(self.focused_pane(), False)

    def action_all_but_newest(self) -> None:
        self.app_.ask_all_but_newest()

    def action_prune(self) -> None:
        self.app_.open_prune()

    def action_protect(self) -> None:
        self.app_.toggle_protect()

    def action_delete(self) -> None:
        self.app_.delete_selection()

    def action_sort(self) -> None:
        self.app_.cycle_sort()

    def action_rescan(self) -> None:
        self.app_.rescan()

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_cursor(self, direction: str) -> None:
        table = self.focused
        if isinstance(table, DataTable):
            table.action_cursor_down() if direction == "down" else table.action_cursor_up()

    def action_filter(self) -> None:
        f = self.query_one("#filter", Input)
        f.display = True
        f.focus()

    def action_close_filter(self) -> None:
        f = self.query_one("#filter", Input)
        if f.display:
            f.display = False
            self.query_one("#titles", DataTable).focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.app_.set_filter(event.value)

    @on(Input.Submitted, "#filter")
    def filter_submitted(self) -> None:
        f = self.query_one("#filter", Input)
        f.display = False
        self.query_one("#titles", DataTable).focus()

    @on(DataTable.RowHighlighted, "#titles")
    def title_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value != self.app_.current_title_key:
            self.app_.current_title_key = event.row_key.value
            self.app_.fill_snapshots()

    def focused_pane(self) -> str:
        return "snapshots" if self.focused is self.query_one("#snapshots", DataTable) else "titles"


# The app

class CheckpointApp(App):
    TITLE = "Checkpoint Manager"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #panes { height: 1fr; }
    #titles { width: 1fr; }
    #snapshots { width: 44; }
    #status { height: 1; background: $panel; color: $text; padding: 0 1; }
    #filter { height: 3; }
    .dialog { width: 72; max-width: 100%; height: auto; max-height: 100%;
              border: thick $primary; background: $surface; padding: 0 1; }
    .dialog.wide { width: 86; }
    .dialog.small { width: 44; }
    ModalScreen { align: center middle; }
    .title { text-style: bold; padding: 0 0 1 0; }
    PruneDialog .title { padding: 0; margin: 0 0 1 0; }
    .error { color: $error; }
    .buttons { height: auto; padding: 1 0 0 0; }
    .buttons Button { margin: 0 1 0 0; }
    .row { height: 1; margin: 0 0 1 0; }
    .field { width: 16; }
    .row Input, .row Select { width: 1fr; }
    #preview { padding: 1 0 0 0; height: auto; }
    """

    def __init__(self, root_arg: str | None, platform: str, state: State, trash: Path | None):
        super().__init__()
        self.root_arg = root_arg
        self.platform = platform
        self.state = state
        self.trash = trash
        self.root: CheckpointRoot | None = None
        self.size_cache: dict[str, tuple[int, int]] = {}
        self.selected: set[str] = set()
        self.current_title_key: str | None = None
        self.filter_text = ""
        self.sizing_done = 0
        self.root_error_shown = False
        self.labels_layout = False

    def on_mount(self) -> None:
        self.push_screen(MainScreen())
        self.set_interval(3.0, self.check_root_alive)
        self.call_after_refresh(self.startup)

    def startup(self) -> None:
        if self.root_arg:
            self.open_path(self.root_arg, self.platform, from_user=True)
        elif self.state.last_root:
            self.open_path(self.state.last_root, self.platform, from_user=False)
        else:
            self.prompt_path()

    # Opening a root

    def prompt_path(self, message: str = "") -> None:
        def done(value: str | None) -> None:
            if value is None:
                if self.root is None:
                    self.exit(0)
                return
            self.open_path(value, "auto", from_user=True)
        self.push_screen(PathPrompt(message, self.state.last_root or ""), done)

    def open_path(self, path: str, platform: str, from_user: bool) -> None:
        try:
            resolved = scan.resolve_root(path, platform)
        except scan.AmbiguousRootError:
            def chosen(plat: str | None) -> None:
                if plat is None:
                    if self.root is None:
                        self.prompt_path()
                    return
                self.open_path(path, plat, from_user)
            self.push_screen(ChoosePlatform(), chosen)
            return
        except scan.RootError as e:
            prefix = "" if from_user else "The last used card is not available. "
            self.prompt_path(prefix + str(e))
            return
        self.load(resolved)

    def load(self, path: Path, keep_cursor: bool = False) -> bool:
        try:
            root = scan.load_root(path)
        except (scan.RootError, OSError) as e:
            self.show_root_error(str(e))
            return False
        for t in root.titles:
            for i, s in enumerate(t.snapshots):
                cached = self.size_cache.get(s.key)
                if cached:
                    t.snapshots[i] = Snapshot(s.category, s.title_dir, s.name, s.timestamp, s.label,
                                              s.path, cached[0], cached[1])
        if self.root is None or self.root.path != root.path:
            self.selected.clear()
            if self.root is not None:
                self.size_cache = {}
        self.root = root
        self.root_error_shown = False
        existing = {s.key for s in root.all_snapshots()}
        self.selected &= existing
        self.state.last_root = str(path)
        self.state.drop_missing(existing, {t.key for t in root.titles})
        self.save()
        if not keep_cursor:
            self.current_title_key = None
        self.fill_titles()
        self.start_sizing()
        if root.skipped:
            self.notify(f"{len(root.skipped)} unrecognised entr{'y' if len(root.skipped) == 1 else 'ies'} "
                        "skipped. They are never offered for deletion. `list` shows them.", timeout=6)
        return True

    def rescan(self) -> None:
        if self.root is not None:
            self.load(self.root.path, keep_cursor=True)

    def show_root_error(self, message: str) -> None:
        if self.root_error_shown:
            return
        self.root_error_shown = True

        def done(choice: str) -> None:
            self.root_error_shown = False
            if choice == "quit":
                self.exit(0)
            elif choice == "other":
                self.prompt_path()
            elif self.root is not None:
                self.load(self.root.path, keep_cursor=True)
        self.push_screen(ErrorScreen(message), done)

    def check_root_alive(self) -> None:
        if self.root is not None and not self.root_error_shown and not scan.is_checkpoint_root(self.root.path):
            self.workers.cancel_group(self, "sizing")
            self.show_root_error(f"{self.root.path} is no longer there. Was the card removed?")

    def save(self) -> None:
        try:
            save_state(self.state)
        except OSError as e:
            self.notify(f"Could not save state: {e.strerror or e}", severity="warning")

    # Sizing in the background

    def start_sizing(self) -> None:
        self.workers.cancel_group(self, "sizing")
        self.sizing_done = sum(1 for s in self.root.all_snapshots() if s.measured)
        self.size_titles(self.root)

    @work(thread=True, exclusive=True, group="sizing")
    def size_titles(self, root: CheckpointRoot) -> None:
        worker = get_current_worker()
        # Biggest wins first: current title, then extdata (where the space is).
        titles = sorted(root.titles, key=lambda t: (t.key != self.current_title_key, t.category != "extdata"))
        for t in titles:
            measured = []
            for s in t.snapshots:
                if worker.is_cancelled:
                    return
                if not s.measured:
                    measured.append(scan.measure(s))
            if measured:
                self.call_from_thread(self.apply_sizes, root, t.key, measured)

    def apply_sizes(self, root: CheckpointRoot, title_key: str, measured: list[Snapshot]) -> None:
        if root is not self.root:
            return
        for s in measured:
            root.update_snapshot(s)
            self.size_cache[s.key] = (s.size_bytes, s.file_count)
        self.sizing_done += len(measured)
        title = root.title_by_key(title_key)
        self.refresh_title_row(title)
        if self.sizing_done >= len(root.all_snapshots()) and self.state.sort == "size":
            self.fill_titles()   # sizes are known now, so the size sort means something
        elif title_key == self.current_title_key:
            self.fill_snapshots()
        self.update_status()

    # Tables

    @property
    def main(self) -> MainScreen:
        for s in self.screen_stack:
            if isinstance(s, MainScreen):
                return s
        raise RuntimeError("main screen missing")

    @property
    def titles_table(self) -> DataTable:
        return self.main.query_one("#titles", DataTable)

    @property
    def snaps_table(self) -> DataTable:
        return self.main.query_one("#snapshots", DataTable)

    def visible_titles(self) -> list[Title]:
        if self.root is None:
            return []
        titles = self.root.titles
        if self.filter_text:
            f = self.filter_text.casefold()
            titles = [t for t in titles if f in t.display_name.casefold() or f in (t.title_id or "").casefold()]
        order = self.state.sort
        if order == "name":
            key = lambda t: (t.display_name.casefold(), t.category)
        elif order == "count":
            key = lambda t: (-len(t.snapshots), t.display_name.casefold())
        elif order == "newest":
            key = lambda t: (-(t.newest.timestamp() if t.newest else 0), t.display_name.casefold())
        else:
            key = lambda t: (-t.total_size, -len(t.snapshots), t.display_name.casefold())
        return sorted(titles, key=key)

    # Column layout. Cells carry their own one-space gap (cell_padding=0),
    # so the panes fit an 80x24 terminal: right pane 44, left gets the rest.

    SNAP_PANE = 44
    LABEL_W = 13

    def pane_widths(self, has_labels: bool) -> tuple[int, int]:
        right = self.SNAP_PANE + (self.LABEL_W if has_labels else 0)
        total = max(self.size.width, 60)
        right = min(right, total - 30)
        return total - right, right

    def on_resize(self) -> None:
        if self.root is not None:
            self.fill_titles()

    def title_size_cell(self, t: Title) -> str:
        return f"{format_size(t.total_size) if t.fully_measured else '...':>8} "

    def fill_titles(self) -> None:
        table = self.titles_table
        table.clear(columns=True)
        left, _ = self.pane_widths(self.labels_layout)
        show_cat = self.root is not None and len(self.root.categories) > 1
        show_id = left >= 58
        name_w = left - 2 - (2 if show_cat else 0) - (8 if show_id else 0) - 6 - 9
        name_w = max(name_w, 8)
        if show_cat:
            table.add_column("C", key="cat", width=2)
        if show_id:
            table.add_column("ID", key="id", width=8)
        table.add_column("Name", key="name", width=name_w)
        table.add_column(" Snaps", key="count", width=6)
        table.add_column("     Size", key="size", width=9)
        titles = self.visible_titles()
        for t in titles:
            style = "dim" if not t.snapshots else ""
            cells = []
            if show_cat:
                cells.append(Text("S" if t.category == "saves" else "E", style=style))
            if show_id:
                cells.append(Text(t.title_id or "-", style=style))
            cells += [Text(trunc(t.display_name, name_w - 1), style=style),
                      Text(f"{len(t.snapshots):>5} ", style=style),
                      Text(self.title_size_cell(t), style=style)]
            table.add_row(*cells, key=t.key)
        if titles:
            keys = [t.key for t in titles]
            row = keys.index(self.current_title_key) if self.current_title_key in keys else 0
            self.current_title_key = keys[row]
            table.move_cursor(row=row)
        else:
            self.current_title_key = None
        self.fill_snapshots()

    def refresh_title_row(self, title: Title) -> None:
        try:
            self.titles_table.update_cell(title.key, "size", Text(self.title_size_cell(title)))
        except Exception:
            pass

    def current_title(self) -> Title | None:
        if self.root is None or self.current_title_key is None:
            return None
        return self.root.title_by_key(self.current_title_key)

    def fill_snapshots(self) -> None:
        table = self.snaps_table
        cursor = table.cursor_row
        table.clear(columns=True)
        title = self.current_title()
        has_labels = bool(title and any(s.label for s in title.snapshots))
        if has_labels != self.labels_layout:
            self.labels_layout = has_labels
            self.fill_titles()   # left pane narrows or widens; it calls back here
            return
        _, right = self.pane_widths(has_labels)
        table.styles.width = right
        table.add_column(" ", key="sel", width=4)
        table.add_column("Taken", key="time", width=20)
        if has_labels:
            table.add_column("Label", key="label", width=self.LABEL_W)
        table.add_column("     Size", key="size", width=10)
        table.add_column("Files", key="files", width=6)
        table.add_column("", key="flags", width=2)
        if title is not None:
            incomplete = rules.incomplete_keys(title)
            protected = self.state.protected_set
            for s in title.snapshots:
                cells = [
                    Text("[x]" if s.key in self.selected else "[ ]",
                         style="bold green" if s.key in self.selected else ""),
                    Text(s.timestamp.strftime("%Y-%m-%d %H:%M:%S")),
                    Text(f"{format_size(s.size_bytes) if s.measured else '...':>9} "),
                    Text(f"{s.file_count if s.measured else '':>5} "),
                    Text(("P" if s.key in protected else "") + ("!" if s.key in incomplete else ""),
                         style="bold yellow"),
                ]
                if has_labels:
                    cells.insert(2, Text(trunc(s.label.strip(), self.LABEL_W - 1)))
                table.add_row(*cells, key=s.key)
            if title.snapshots:
                table.move_cursor(row=min(max(cursor, 0), len(title.snapshots) - 1), scroll=False)
            self.refresh_title_row(title)
        self.update_status()

    def update_status(self) -> None:
        if self.root is None:
            self.main.query_one("#status", Static).update("No card open")
            return
        snaps = self.root.all_snapshots()
        total = sum(s.size_bytes or 0 for s in snaps)
        sizing = "" if self.sizing_done >= len(snaps) else f" (sizing {self.sizing_done}/{len(snaps)})"
        sel = [s for s in snaps if s.key in self.selected]
        sel_size = sum(s.size_bytes or 0 for s in sel)
        plat = {"3ds": "3DS", "switch": "Switch"}.get(self.root.platform, "unknown")
        trash = " | trash on" if self.trash else ""
        left = f"{plat} {self.root.path}"
        right = (f" | {len(snaps)} snaps {format_size(total)}{sizing}"
                 f" | sel {len(sel)} {format_size(sel_size)}{trash} | ? help")
        width = max(self.size.width - 2, 20)
        left = trunc(left, max(width - len(right), 12))
        self.main.query_one("#status", Static).update(left + right)

    # Selection

    def _redraw_selection(self) -> None:
        self.fill_snapshots()

    def toggle_current(self, pane: str) -> None:
        title = self.current_title()
        if title is None:
            return
        protected = self.state.protected_set
        if pane == "snapshots":
            table = self.snaps_table
            if not title.snapshots:
                return
            s = title.snapshots[table.cursor_row]
            if s.key in protected:
                self.notify("Protected. Press P to un-protect it first.", severity="warning")
                return
            self.selected ^= {s.key}
            self._redraw_selection()
            if table.cursor_row < len(title.snapshots) - 1:
                table.move_cursor(row=table.cursor_row + 1)
        else:
            keys = {s.key for s in title.snapshots if s.key not in protected}
            if keys and keys <= self.selected:
                self.selected -= keys
            else:
                self.selected |= keys
            self._redraw_selection()

    def select_all(self, pane: str, on: bool) -> None:
        protected = self.state.protected_set
        if pane == "snapshots":
            title = self.current_title()
            keys = {s.key for s in title.snapshots} if title else set()
        else:
            keys = {s.key for t in self.visible_titles() for s in t.snapshots}
        if on:
            self.selected |= keys - protected
        else:
            self.selected -= keys
        self._redraw_selection()

    def ask_all_but_newest(self) -> None:
        title = self.current_title()
        if title is None:
            return

        def done(n: int | None) -> None:
            if n is None:
                return
            self.state.default_keep = n
            self.save()
            title_now = self.current_title()
            if title_now is None:
                return
            keys = {s.key for s in title_now.snapshots}
            picks = {s.key for s in rules.all_but_newest(title_now, n, self.state.protected_set)}
            self.selected = (self.selected - keys) | picks
            self._redraw_selection()
            self.notify(f"Selected {len(picks)} of {len(keys)} in {trunc(title_now.display_name, 30)}.")
        self.push_screen(AskNumber(f"Select all but the newest N in {trunc(title.display_name, 24)}",
                                   self.state.default_keep), done)

    def toggle_protect(self) -> None:
        title = self.current_title()
        if title is None or not title.snapshots:
            return
        s = title.snapshots[self.snaps_table.cursor_row]
        on = s.key not in self.state.protected_set
        self.state.set_protected(s.key, on)
        if on:
            self.selected.discard(s.key)
        self.save()
        self._redraw_selection()
        self.notify(("Protected " if on else "Un-protected ") + s.name)

    def cycle_sort(self) -> None:
        i = SORT_ORDERS.index(self.state.sort) if self.state.sort in SORT_ORDERS else 0
        self.state.sort = SORT_ORDERS[(i + 1) % len(SORT_ORDERS)]
        self.save()
        self.fill_titles()
        self.notify(f"Sorted by {self.state.sort}", timeout=2)

    def set_filter(self, text: str) -> None:
        self.filter_text = text.strip()
        self.fill_titles()

    # Prune and delete

    def open_prune(self) -> None:
        if self.root is None:
            return

        def done(result) -> None:
            if not result:
                return
            action, snaps = result
            self.selected = {s.key for s in snaps}
            self._redraw_selection()
            if action == "delete":
                self.confirm_and_delete(snaps)
            else:
                self.notify(f"Selected {len(snaps)} snapshot(s). Review, then press d to delete.")
        self.push_screen(PruneDialog(self.root, self.current_title(), self.state.protected_set,
                                     self.state.default_keep), done)

    def delete_selection(self) -> None:
        if self.root is None:
            return
        snaps = [s for s in self.root.all_snapshots() if s.key in self.selected]
        if not snaps:
            self.notify("Nothing selected. Space selects, o selects all but the newest N.")
            return
        self.confirm_and_delete(snaps)

    def confirm_and_delete(self, snaps: list[Snapshot]) -> None:
        protected = self.state.protected_set
        locked = [s for s in snaps if s.key in protected]
        if locked:
            self.notify(f"{len(locked)} selected snapshot(s) are protected. Un-protect them first.",
                        severity="error")
            return
        titles = {t.key: t for t in self.root.titles}
        trash_offer = self.trash
        if trash_offer is not None:
            try:
                deleter.verify_trash_dir(self.root.path, trash_offer)
            except deleter.SafetyError as e:
                self.notify(f"Trash folder not usable, Move is off: {e}", severity="warning", timeout=8)
                trash_offer = None

        def confirmed(choice: str | None) -> None:
            if not choice:
                return
            trash = self.trash if choice == "move" else None
            self.push_screen(ProgressScreen(self.root.path, snaps, trash), finished)

        def finished(result: deleter.RunResult | None) -> None:
            if result is not None:
                self.selected -= set(result.deleted)
                for key in result.deleted:
                    self.size_cache.pop(key, None)
            self.rescan()
        self.push_screen(ConfirmDelete(snaps, titles, trash_offer), confirmed)


def run_tui(args) -> int:
    state = load_state()
    if getattr(args, "clear_trash_dir", False):
        state.trash_dir = None
    if getattr(args, "trash_dir", None):
        state.trash_dir = str(Path(args.trash_dir).expanduser())
    try:
        save_state(state)
    except OSError:
        pass
    trash = Path(state.trash_dir) if state.trash_dir else None
    root_arg = getattr(args, "root_opt", None) or getattr(args, "root", None)
    app = CheckpointApp(root_arg, getattr(args, "platform", "auto"), state, trash)
    app.run()
    return app.return_code or 0
