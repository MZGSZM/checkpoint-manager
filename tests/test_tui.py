"""Pilot smoke tests (spec 13). Run headless at 80x24."""

import asyncio

from conftest import tree
from state import load_state
from tui import CheckpointApp, ConfirmDelete, MainScreen, PathPrompt, ProgressScreen, PruneDialog


def run_app(root, script, trash=None):
    async def go():
        app = CheckpointApp(str(root) if root else None, "auto", load_state(), trash)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.2)
            await app.workers.wait_for_complete()
            await pilot.pause(0.1)
            await script(app, pilot)
        return app
    return asyncio.run(go())


def test_opens_and_sizes(small):
    async def script(app, pilot):
        assert app.titles_table.row_count == 12
        assert app.current_title_key == "extdata/0x0008F Theme"   # biggest first
        assert all(s.measured for s in app.root.all_snapshots())
        assert app.snaps_table.row_count == 24
    run_app(small, script)


def test_all_but_newest_then_cancel_deletes_nothing(small):
    before = tree(small)

    async def script(app, pilot):
        await pilot.press("o")
        await pilot.pause()
        await pilot.press("enter")           # default N = 4
        await pilot.pause()
        assert len(app.selected) == 20
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDelete)
        assert app.focused.id == "cancel"
        await pilot.press("enter")           # default focus is Cancel
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    run_app(small, script)
    assert tree(small) == before


def test_all_but_newest_and_delete(small):
    async def script(app, pilot):
        for key in ("o", "enter", "d"):
            await pilot.press(key)
            await pilot.pause()
        await pilot.press("tab")             # Cancel -> Delete
        await pilot.press("enter")
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        assert isinstance(app.screen, ProgressScreen)
        assert len(app.screen.result.deleted) == 20
        await pilot.press("enter")           # Close
        await pilot.pause(0.2)
        assert app.selected == set()
    run_app(small, script)
    remaining = sorted(p.name for p in (small / "extdata" / "0x0008F Theme").iterdir())
    assert remaining == ["20251206-155202", "20251206-155208", "20261008-145347", "20261008-145355"]


def test_prune_preview_and_select(small):
    before = tree(small)

    async def script(app, pilot):
        await pilot.press("p")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, PruneDialog)
        assert len(dialog.plan.delete) == 48
        await pilot.press("enter")           # Select is the default
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert len(app.selected) == 48
    run_app(small, script)
    assert tree(small) == before


def test_protect_blocks_selection_and_delete(small):
    async def script(app, pilot):
        await pilot.press("tab")             # snapshot pane
        await pilot.press("P")
        key = app.current_title().snapshots[0].key
        assert key in app.state.protected_set
        await pilot.press("space")
        assert key not in app.selected
        await pilot.press("a")
        assert key not in app.selected and len(app.selected) == 23
        assert key in load_state().protected_set   # persisted
    run_app(small, script)


def test_filter_typing_does_not_trigger_keys(small):
    async def script(app, pilot):
        await pilot.press("slash")
        await pilot.press("q", "u", "i", "d")   # 'q' and 'd' must type, not act
        await pilot.pause()
        assert app.is_running
        assert isinstance(app.screen, MainScreen)
        await pilot.press(*"mario")
        await pilot.pause()
        names = {app.root.title_by_key(k.value).display_name for k in app.titles_table.rows}
        assert names == set()                    # "quidmario" matches nothing
    run_app(small, script)


def test_big_delete_needs_typing(small):
    async def script(app, pilot):
        await pilot.press("p")
        await pilot.pause()
        app.screen.query_one("#delete").press()
        await pilot.pause()
        confirm = app.screen
        assert isinstance(confirm, ConfirmDelete) and confirm.big
        assert confirm.query_one("#delete").disabled
        confirm.query_one("#typed").focus()
        await pilot.press(*"delete")
        await pilot.pause()
        assert not confirm.query_one("#delete").disabled
        await pilot.press("escape")
    before = tree(small)
    run_app(small, script)
    assert tree(small) == before


def test_no_root_prompts(tmp_path):
    async def script(app, pilot):
        assert isinstance(app.screen, PathPrompt)
    run_app(None, script)


def test_switch_root_hides_category(switch):
    async def script(app, pilot):
        keys = [c.value for c in app.titles_table.columns]
        assert "cat" not in keys
        assert "Switch" in str(app.main.query_one("#status").render())
    run_app(switch, script)


def test_card_removed_mid_session(small):
    import shutil
    from tui import ErrorScreen

    async def script(app, pilot):
        shutil.rmtree(small)
        app.check_root_alive()
        await pilot.pause()
        assert isinstance(app.screen, ErrorScreen)
        app.check_root_alive()               # no second dialog stacked
        await pilot.pause()
        assert sum(isinstance(s, ErrorScreen) for s in app.screen_stack) == 1
        await pilot.click("#rescan")
        await pilot.pause()
        assert isinstance(app.screen, ErrorScreen)   # still gone, still offered
        await pilot.click("#quit")
        await pilot.pause()
    app = run_app(small, script)
    assert not app.is_running


def test_label_title_relayout(small):
    async def script(app, pilot):
        keys = [k.value for k in app.titles_table.rows]
        row = keys.index("saves/0x019BD Mario Party  Star Rush")
        app.titles_table.move_cursor(row=row)
        await pilot.pause()
        assert "label" in [c.value for c in app.snaps_table.columns]
        assert app.labels_layout
        app.titles_table.move_cursor(row=0)
        await pilot.pause()
        assert not app.labels_layout
    run_app(small, script)


def test_size_sort_after_sizing(small):
    async def script(app, pilot):
        keys = [k.value for k in app.titles_table.rows]
        sizes = [app.root.title_by_key(k).total_size for k in keys]
        assert sizes == sorted(sizes, reverse=True)
    run_app(small, script)
