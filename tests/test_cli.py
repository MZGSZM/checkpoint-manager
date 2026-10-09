import json

import fixtures
import main
from conftest import tree
from state import load_state, save_state


def run(capsys, *argv):
    code = main.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return code, out, err


def run_json(capsys, *argv):
    code, out, err = run(capsys, *argv, "--json")
    return code, json.loads(out), err


def test_list_json(small, capsys):
    code, data, _ = run_json(capsys, "list", small)
    assert code == 0
    assert len(data["snapshots"]) == 73
    row = next(r for r in data["snapshots"] if r["snapshot"] == "20250813-214417 before boss")
    assert row["label"] == " before boss" and row["title_id"] == "0x019BD"
    assert row["timestamp"] == "2025-08-13T21:44:17"
    assert any(s["path"].endswith("notes") for s in data["skipped"])
    incomplete = {r["snapshot"] for r in data["snapshots"] if r["possibly_incomplete"]}
    assert incomplete == {"20250630-005137", "20250706-233510"}   # Omega Ruby empty + tiny


def test_list_tsv_and_table(small, capsys):
    code, out, _ = run(capsys, "list", small, "--tsv", "--title", "theme")
    assert code == 0
    lines = out.strip().splitlines()
    assert lines[0].startswith("category\ttitle_id") and len(lines) == 25
    code, out, _ = run(capsys, "list", small, "--no-color", "--category", "saves")
    assert code == 0 and "Theme" not in out and "MARIO KART 7" in out


def test_stats_json_sorted(small, capsys):
    code, data, _ = run_json(capsys, "stats", small)
    assert code == 0
    sizes = [t["size_bytes"] for t in data["titles"]]
    assert sizes == sorted(sizes, reverse=True)
    assert data["totals"]["snapshots"] == 73
    assert data["titles"][0]["title_dir"] == "0x0008F Theme"


def test_prune_dry_run_changes_nothing(small, capsys):
    before = tree(small)
    code, out, _ = run(capsys, "prune", small, "--keep-newest", "4", "--no-color")
    assert code == 0 and "Dry run" in out
    assert tree(small) == before


def test_prune_apply_deletes_exactly_the_plan(small, capsys):
    code, plan, _ = run_json(capsys, "prune", small, "--keep-newest", "4")
    planned = {f"{s['category']}/{s['title_dir']}/{s['snapshot']}" for s in plan["plan"]["delete"]}
    assert plan["dry_run"] and plan["deleted"] == [] and len(planned) == 20 + 8 + 8 + 8 + 4
    before = tree(small)
    code, res, _ = run_json(capsys, "prune", small, "--keep-newest", "4", "--apply", "--yes")
    assert code == 0 and set(res["deleted"]) == planned and res["failed"] == []
    gone = before - tree(small)
    assert {g for g in gone if g.count("/") == 2} == planned
    assert all(any(g == p or g.startswith(p + "/") for p in planned) for g in gone)
    assert res["bytes_reclaimed"] > 0


def test_prune_needs_rule(small, capsys):
    code, _, err = run(capsys, "prune", small)
    assert code == 2 and "keep-newest" in err


def test_prune_bad_age(small, capsys):
    code, _, err = run(capsys, "prune", small, "--older-than", "soon")
    assert code == 2


def test_keep_min_zero_needs_allow(small, capsys):
    before = tree(small)
    code, _, err = run(capsys, "prune", small, "--keep-newest", "0", "--keep-min", "0", "--apply", "--yes")
    assert code == 3 and tree(small) == before
    code, _, _ = run(capsys, "prune", small, "--keep-newest", "0", "--keep-min", "0",
                     "--apply", "--yes", "--allow-delete-all", "--title", "theme")
    assert code == 0
    assert not any((small / "extdata" / "0x0008F Theme").iterdir())


def test_apply_without_yes_on_pipe(small, capsys):
    before = tree(small)
    code, _, err = run(capsys, "prune", small, "--keep-newest", "4", "--apply")
    assert code == 2 and "--yes" in err and tree(small) == before


def test_protected_respected(small, capsys):
    st = load_state()
    key = "extdata/0x0008F Theme/20250520-213146"
    st.protected = [key]
    save_state(st)
    code, res, _ = run_json(capsys, "prune", small, "--keep-newest", "4", "--title", "0x0008F",
                            "--apply", "--yes")
    assert key not in res["deleted"] and len(res["deleted"]) == 19
    assert (small / key).exists()
    code, _, err = run(capsys, "delete", small, "--title", "theme", "--snapshot", "20250520-213146",
                       "--apply", "--yes")
    assert code == 3 and (small / key).exists()


def test_delete_command(small, capsys):
    code, res, _ = run_json(capsys, "delete", small, "--title", "0x0008F",
                            "--snapshot", "20250520-213146", "--snapshot", "20250520-213152")
    assert code == 0 and res["dry_run"] and res["plan"]["delete_count"] == 2
    assert (small / "extdata/0x0008F Theme/20250520-213146").exists()
    code, res, _ = run_json(capsys, "delete", small, "--title", "0x0008F",
                            "--snapshot", "20250520-213146", "--apply", "--yes")
    assert code == 0 and res["deleted"] == ["extdata/0x0008F Theme/20250520-213146"]
    code, _, err = run(capsys, "delete", small, "--title", "0x0008F", "--snapshot", "nope")
    assert code == 2
    # MK7 has the same snapshot name in saves and extdata
    code, _, err = run(capsys, "delete", small, "--title", "0x00308", "--snapshot", "20250520-212712")
    assert code == 2 and "--category" in err
    code, _, _ = run(capsys, "delete", small, "--title", "0x00308", "--category", "saves",
                     "--snapshot", "20250520-212712", "--apply", "--yes")
    assert code == 0 and (small / "extdata/0x00308 MARIO KART 7/20250520-212712").exists()
    code, _, err = run(capsys, "delete", small, "--snapshot", "20250520-212712")
    assert code == 2


def test_root_errors(tmp_path, capsys):
    code, _, err = run(capsys, "list", tmp_path / "nowhere")
    assert code == 2
    code, _, err = run(capsys, "list", tmp_path)
    assert code == 2
    code, _, err = run(capsys, "list")   # nothing remembered
    assert code == 2


def test_last_root_remembered(small, capsys):
    run(capsys, "stats", small)
    code, data, _ = run_json(capsys, "stats")
    assert code == 0 and data["root"] == str(small)


def test_sd_root_and_platform(small, capsys):
    sd = small.parent.parent
    code, data, _ = run_json(capsys, "stats", sd)
    assert code == 0 and data["platform"] == "3ds"
    fixtures.build_switch(sd)
    code, _, err = run(capsys, "stats", sd)
    assert code == 2 and "--platform" in err
    code, data, _ = run_json(capsys, "stats", sd, "--platform", "switch")
    assert code == 0 and data["platform"] == "switch"


def test_switch_root(switch, capsys):
    code, data, _ = run_json(capsys, "list", switch)
    assert code == 0 and {r["category"] for r in data["snapshots"]} == {"saves"}
    code, data, _ = run_json(capsys, "list", switch, "--category", "extdata")
    assert code == 0 and data["snapshots"] == []
    code, out, err = run(capsys, "prune", switch, "--category", "extdata", "--keep-newest", "1")
    assert code == 0 and "Nothing to do" in out + err
    code, res, _ = run_json(capsys, "prune", switch, "--keep-newest", "2", "--apply", "--yes")
    assert code == 0 and len(res["deleted"]) == 4 * 4 + 1
    assert (switch / "saves" / "01006F8002326000 Animal Crossing" / "Player1").is_dir()
    code, out, _ = run(capsys, "stats", switch, "--no-color")
    assert "Cat" not in out   # no category column on a saves-only root


def test_logs_command(small, capsys):
    code, res, _ = run_json(capsys, "logs", small, "--older-than", "2025-06-01")
    assert code == 0 and sorted(res["plan"]["delete"]) == ["logs/checkpoint_20250427.log",
                                                           "logs/checkpoint_20250520.log"]
    code, res, _ = run_json(capsys, "logs", small, "--older-than", "2025-06-01", "--apply", "--yes")
    assert code == 0 and len(res["deleted"]) == 2
    assert sorted(p.name for p in (small / "logs").iterdir()) == ["checkpoint_20261008.log", "other.txt"]


def test_json_keeps_stdout_clean(small, capsys):
    code, out, err = run(capsys, "prune", small, "--keep-newest", "4", "--keep-min", "0", "--json")
    json.loads(out)
    assert "Warning" in err


def test_no_subcommand_means_tui():
    assert main.normalise_argv([]) == ["tui"]
    assert main.normalise_argv(["/some/path"]) == ["tui", "/some/path"]
    assert main.normalise_argv(["list", "x"]) == ["list", "x"]


def test_cards_dont_wipe_each_others_protection(small, switch, capsys):
    st = load_state()
    st.protected = ["extdata/0x0008F Theme/20250520-213146", "extdata/0x0008F Theme/19990101-000000"]
    save_state(st)
    run(capsys, "stats", switch)
    assert len(load_state().protected) == 2      # other card's titles untouched
    run(capsys, "stats", small)
    assert load_state().protected == ["extdata/0x0008F Theme/20250520-213146"]
