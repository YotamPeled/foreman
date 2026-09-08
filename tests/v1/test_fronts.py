"""`foreman front add|list|prefer|close` and the status lines they feed.

Every test drives the real entry points against a fresh FOREMAN_STATE and
FOREMAN_CONFIG directory, then reads the ledgers back. No mocks: the break
each test catches is a wrong branch, a missing side effect, or a missing
refusal in the verbs themselves. The two repository fixtures both add
cleanly; every other brief here is a minimal literal mutated one rule at a
time, so a passing suite means each refusal names its rule.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from foreman import cli, entities, fronts, ids, paths, store
from foreman.caller import SESSION_ENV

ROOT = Path(__file__).resolve().parents[2]
PANEL_BRIEF = ROOT / "briefs" / "panel"
RUNTIME_BRIEF = ROOT / "briefs" / "runtime-v1"

#: A minimal brief that validates: two tasks chained first -> second, one
#: monitor, two allocated roles. Tests mutate one rule at a time out of it.
VALID_BRIEF = '''name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 2
opus = 1

[[task]]
title = "first work"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "make check first"
size = 3
after = []

[[task]]
title = "second work"
scope = """
WHAT: do the second thing.
INPUTS: the first artifact.
OUTPUTS: the second artifact.
OUT OF SCOPE: everything else.
"""
verify = "make check second"
size = 2
after = ["first work"]

[[monitor]]
question = "how much is done?"
measure = "count-things"
unit = "things"
every = "landing"
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    # `front add` refuses a `land-on` branch it cannot find, so every brief
    # written under tmp_path lives in a throwaway repository with a `main`
    # branch; the branch test that needs no repository builds its own dir.
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


def write_brief(root: Path, name: str, text: str | None = None,
                plan: str | None = None) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        VALID_BRIEF.format(name=name) if text is None else text,
        encoding="utf-8")
    if plan is not None:
        (brief_dir / "plan.md").write_text(plan, encoding="utf-8")
    return brief_dir


def add_ok(brief_dir: Path) -> None:
    assert fronts.front_add_main(str(brief_dir)) == 0


def add_refused(capsys, brief_dir: Path, *markers: str) -> str:
    """A refused add names every marker on one stderr line and writes nothing."""
    rc = fronts.front_add_main(str(brief_dir))
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert err.count("refused:") == 1
    for marker in markers:
        assert marker in err, f"{marker!r} not named in: {err.strip()}"
    return err


def test_panel_and_runtime_v1_briefs_validate_and_add(env, capsys):
    """Both repository fixtures add cleanly: records, tasks, copied briefs.

    runtime-v1 waits on a `runtime` front, so a minimal one is added first,
    the way the real ledger would already hold it. A missing task state, a
    wrong unit count or a lost brief copy fails here.
    """
    add_ok(write_brief(env, "runtime"))
    capsys.readouterr()
    assert fronts.front_add_main(str(PANEL_BRIEF)) == 0
    assert fronts.front_add_main(str(RUNTIME_BRIEF)) == 0

    record = store.fold_by_id(
        store.read_ledger(paths.front_record_path("panel")))[0]
    assert record["state"] == "queued"
    assert record["brief_path"] == str(paths.brief_path("panel"))
    assert Path(record["brief_path"]).read_text(
        encoding="utf-8") == (PANEL_BRIEF / "brief.toml").read_text(
        encoding="utf-8")
    assert record["monitors"][0]["measure"] == \
        "foreman-verify panel --blocks-count"

    tasks = {task["title"]: task for task in store.read_ledger(
        paths.front_tasks_path("panel"))}
    assert len(tasks) == 4
    assert len({task["id"] for task in tasks.values()}) == 4
    first = tasks["plugin skeleton and data feed"]
    assert first["state"] == "ready"
    assert first["after"] == []
    assert (first["units_done"], first["units_total"]) == (0, 1)
    assert first["front"] == "panel"
    assert first["core"] is False
    second = tasks["blocks in the v0 layout"]
    assert second["state"] == "waiting"
    assert second["after"] == ["plugin skeleton and data feed"]
    assert (second["units_done"], second["units_total"]) == (0, 7)

    assert len(store.read_ledger(paths.front_tasks_path("runtime-v1"))) == 6
    _assert_task_contract(PANEL_BRIEF, "panel")
    _assert_task_contract(RUNTIME_BRIEF, "runtime-v1")


def _assert_task_contract(brief_dir: Path, front: str) -> None:
    """Every stored task keeps the brief's worker contract verbatim.

    A task that loses its scope, its verify command, its size, its
    predecessors or its timeout still counts and orders correctly, so only an
    exact comparison against the brief file catches the loss.
    """
    data = tomllib.loads((brief_dir / "brief.toml").read_bytes().decode(
        "utf-8"))
    expected = {entry["title"]: entry for entry in data["task"]}
    stored = {task["title"]: task for task in store.read_ledger(
        paths.front_tasks_path(front))}
    assert set(stored) == set(expected)
    for title, entry in expected.items():
        task = stored[title]
        assert task["scope"] == entry["scope"], title
        assert task["verify"] == entry["verify"], title
        assert task["size"] == entry["size"], title
        assert task["units_total"] == entry["size"], title
        assert task["after"] == entry.get("after", []), title
        assert task["timeout"] == entry.get("timeout", ""), title
        assert task["land_on"] == entry.get("land-on", data["land-on"]), title


def test_add_keeps_task_timeout(env):
    """A task's timeout reaches the ledger: dropping it defaults every task
    to the pool timeout, which no count or state assertion catches."""
    text = VALID_BRIEF.format(name="opts").replace(
        "size = 3", 'size = 3\ntimeout = "20m"')
    add_ok(write_brief(env, "opts", text))
    tasks = {task["title"]: task for task in store.read_ledger(
        paths.front_tasks_path("opts"))}
    assert tasks["first work"]["timeout"] == "20m"
    assert tasks["second work"]["timeout"] == ""


def test_add_copies_brief_and_plan(env):
    """The front no longer depends on its source directory: brief bytes and
    plan.md land beside the config, and the record names the copy."""
    brief_dir = write_brief(env, "copied", plan="# why\n")
    add_ok(brief_dir)
    assert paths.brief_path("copied").read_text(
        encoding="utf-8") == (brief_dir / "brief.toml").read_text(
        encoding="utf-8")
    assert paths.plan_path("copied").read_text(encoding="utf-8") == "# why\n"
    record = store.fold_by_id(
        store.read_ledger(paths.front_record_path("copied")))[0]
    assert record["brief_path"] == str(paths.brief_path("copied"))


def test_scope_must_name_all_four_parts(env, capsys):
    """A scope without OUT OF SCOPE is refused by that heading's name."""
    text = VALID_BRIEF.format(name="scope").replace(
        "OUT OF SCOPE: everything else.", "NOT IN SCOPE: everything else.")
    add_refused(capsys, write_brief(env, "scope", text),
                "scope", "OUT OF SCOPE")
    assert not paths.front_record_path("scope").exists()


def test_task_without_verify_is_refused(env, capsys):
    brief_dir = write_brief(env, "noverify", VALID_BRIEF.format(
        name="noverify").replace('verify = "make check first"', 'verify = ""'))
    add_refused(capsys, brief_dir, "verify")


def test_task_without_size_is_refused(env, capsys):
    brief_dir = write_brief(env, "nosize", VALID_BRIEF.format(
        name="nosize").replace("size = 3", "size = 0"))
    add_refused(capsys, brief_dir, "size")


def test_allocation_unknown_role_is_refused(env, capsys):
    brief_dir = write_brief(env, "badrole", VALID_BRIEF.format(
        name="badrole").replace("opus = 1", "wizard = 1"))
    add_refused(capsys, brief_dir, "allocation", "wizard")


def test_allocation_negative_count_is_refused(env, capsys):
    brief_dir = write_brief(env, "negalloc", VALID_BRIEF.format(
        name="negalloc").replace("muse = 2", "muse = -1"))
    add_refused(capsys, brief_dir, "allocation", "muse")


def test_front_after_unknown_front_is_refused(env, capsys):
    brief_dir = write_brief(env, "badafter", VALID_BRIEF.format(
        name="badafter").replace("after     = []",
                                 'after     = ["ghost-front"]'))
    add_refused(capsys, brief_dir, "after", "ghost-front")


def test_task_after_unknown_task_is_refused(env, capsys):
    brief_dir = write_brief(env, "badtaskafter", VALID_BRIEF.format(
        name="badtaskafter").replace('after = ["first work"]',
                                     'after = ["no such task"]'))
    add_refused(capsys, brief_dir, "after", "no such task")


def test_task_after_cycle_is_refused(env, capsys):
    """first waits on second while second waits on first: refused as a cycle,
    naming both titles in the path."""
    brief_dir = write_brief(env, "cycle", VALID_BRIEF.format(
        name="cycle").replace("after = []", 'after = ["second work"]'))
    add_refused(capsys, brief_dir, "cycle", "first work", "second work")


def test_done_when_two_sentences_is_refused(env, capsys):
    brief_dir = write_brief(env, "twosent", VALID_BRIEF.format(
        name="twosent").replace(
        'done-when = "The thing works end to end on this machine."',
        'done-when = "It works. Everyone cheers."'))
    add_refused(capsys, brief_dir, "done-when")


def test_brief_without_name_is_refused(env, capsys):
    text = VALID_BRIEF.format(name="noname").replace(
        'name      = "noname"\n', "")
    add_refused(capsys, write_brief(env, "noname", text), "name")


def test_monitor_without_measure_is_refused(env, capsys):
    brief_dir = write_brief(env, "nomeasure", VALID_BRIEF.format(
        name="nomeasure").replace('measure = "count-things"', 'measure = ""'))
    add_refused(capsys, brief_dir, "measure")


def test_monitor_without_unit_is_refused(env, capsys):
    brief_dir = write_brief(env, "nounit", VALID_BRIEF.format(
        name="nounit").replace('unit = "things"\n', ""))
    add_refused(capsys, brief_dir, "unit")
    assert not paths.front_record_path("nounit").exists()


def test_monitor_without_every_is_refused(env, capsys):
    brief_dir = write_brief(env, "noevery", VALID_BRIEF.format(
        name="noevery").replace('every = "landing"\n', ""))
    add_refused(capsys, brief_dir, "every")
    assert not paths.front_record_path("noevery").exists()


def test_monitor_with_unparseable_alert_is_refused(env, capsys):
    text = VALID_BRIEF.format(name="badalert").replace(
        'every = "landing"', 'every = "landing"\nalert = "nonsense"')
    add_refused(capsys, write_brief(env, "badalert", text), "alert")
    assert not paths.front_record_path("badalert").exists()


def test_monitor_comparison_alerts_add(env):
    """Alerts that parse — a comparison and a number — are admitted."""
    for index, alert in enumerate(["< 0.80", ">= 5", "!= 0", "> 120"]):
        name = f"alertok{index}"
        text = VALID_BRIEF.format(name=name).replace(
            'every = "landing"', f'every = "landing"\nalert = "{alert}"')
        add_ok(write_brief(env, name, text))


def test_monitor_defects_join_one_refusal(env, capsys):
    """A monitor wrong three ways joins the single refusal: the owner fixes
    the monitor in one round, and a task defect alongside still names both."""
    text = VALID_BRIEF.format(name="badmonitor")
    text = text.replace('unit = "things"\n', "")
    text = text.replace('every = "landing"\n', "")
    text = text.replace('verify = "make check first"', 'verify = ""')
    text = text.replace("[[monitor]]", '[[monitor]]\nalert = "nonsense"')
    add_refused(capsys, write_brief(env, "badmonitor", text),
                "unit", "every", "alert", "verify")
    assert not paths.front_record_path("badmonitor").exists()


def test_malformed_task_after_is_refused_with_other_violations(env, capsys):
    """A dependency that is not a string is a refusal, not a traceback: the
    bad entry is named alongside every other violation in the same message."""
    text = VALID_BRIEF.format(name="badaftertype")
    text = text.replace('after = ["first work"]', 'after = [["first work"]]')
    text = text.replace('verify = "make check second"', 'verify = ""')
    add_refused(capsys, write_brief(env, "badaftertype", text),
                "after", "verify")
    assert not paths.front_record_path("badaftertype").exists()


def test_land_on_unknown_branch_is_refused(env, capsys):
    brief_dir = write_brief(env, "badbranch", VALID_BRIEF.format(
        name="badbranch").replace('land-on   = "main"',
                                 'land-on   = "review-no-such-branch-92817"'))
    add_refused(capsys, brief_dir, "land-on", "review-no-such-branch-92817")
    assert not paths.front_record_path("badbranch").exists()


def test_land_on_outside_any_repository_is_refused(env, capsys,
                                                  tmp_path_factory):
    """A brief directory git cannot place gets a refusal naming that, rather
    than a silent pass on a branch nobody checked."""
    brief_dir = write_brief(tmp_path_factory.mktemp("nogit"), "outside")
    add_refused(capsys, brief_dir, "land-on", "not inside a git repository")
    assert not paths.front_record_path("outside").exists()


def test_missing_brief_file_is_refused(env, capsys):
    rc = fronts.front_add_main(str(env / "absent"))
    _, err = capsys.readouterr()
    assert rc == 1
    assert "not found" in err


def test_invalid_toml_is_refused(env, capsys):
    brief_dir = env / "badtoml"
    brief_dir.mkdir(parents=True)
    (brief_dir / "brief.toml").write_text("name = [unclosed\n",
                                          encoding="utf-8")
    rc = fronts.front_add_main(str(brief_dir))
    _, err = capsys.readouterr()
    assert rc == 1
    assert "not valid TOML" in err


def test_three_violations_named_in_one_message(env, capsys):
    """A brief wrong three ways is refused once, naming all three: the owner
    fixes everything in one round instead of one refusal per mistake."""
    text = VALID_BRIEF.format(name="triple")
    text = text.replace("OUT OF SCOPE: everything else.",
                        "NOT IN SCOPE: everything else.")
    text = text.replace("opus = 1", "wizard = 1")
    text = text.replace('done-when = "The thing works end to end on this machine."',
                        'done-when = "It works. Everyone cheers."')
    add_refused(capsys, write_brief(env, "triple", text),
                "OUT OF SCOPE", "wizard", "done-when")


def test_adding_the_same_front_twice_is_refused_by_name(env, capsys):
    """The second add names the front: ledgers are append-only, so a repeated
    add must refuse rather than duplicate the front's lines."""
    add_ok(write_brief(env, "dup"))
    capsys.readouterr()
    rc = fronts.front_add_main(str(env / "dup"))
    _, err = capsys.readouterr()
    assert rc == 1
    assert "dup" in err
    assert len(store.read_ledger(paths.front_record_path("dup"))) == 1


def _tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) + ("/" if path.is_dir() else "")
                  for path in root.rglob("*"))


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Every file under root by relative name, with its exact bytes."""
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_dry_run_writes_nothing(env, monkeypatch, capsys):
    """--dry-run validates and prints what it would write while a populated
    state and config stay identical: same entries and same bytes per file.
    A dry run that corrupts an existing file fails here, not just one that
    creates files under empty directories."""
    monkeypatch.chdir(ROOT)
    add_ok(write_brief(env, "seeded"))
    capsys.readouterr()
    before = ((_tree(env / "state"), _tree(env / "config")),
              (_tree_bytes(env / "state"), _tree_bytes(env / "config")))
    assert cli.main(["front", "add", "briefs/panel", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would write" in out
    assert "panel" in out
    assert ((_tree(env / "state"), _tree(env / "config")),
            (_tree_bytes(env / "state"), _tree_bytes(env / "config"))) == before


def test_prefer_appends_a_revised_copy(env, capsys):
    """Preferring grows the ledger by one line and the fold carries the new
    value under the same record id, monitors included. Rewriting the earlier
    lines while appending fails here: the file must open with exactly the
    bytes it held before the call."""
    add_ok(write_brief(env, "pref"))
    capsys.readouterr()
    before_bytes = paths.front_record_path("pref").read_bytes()
    before = store.read_ledger(paths.front_record_path("pref"))
    assert fronts.front_prefer_main("pref", "7") == 0
    assert capsys.readouterr().out.strip() == "pref prefer 7"
    after_bytes = paths.front_record_path("pref").read_bytes()
    assert after_bytes.startswith(before_bytes)
    assert len(after_bytes) > len(before_bytes)
    after = store.read_ledger(paths.front_record_path("pref"))
    assert len(after) == len(before) + 1
    folded = store.fold_by_id(after)[0]
    assert folded["prefer"] == 7
    assert folded["id"] == before[0]["id"]
    assert folded["state"] == "queued"
    assert folded["monitors"][0]["measure"] == "count-things"


def test_prefer_refusals(env, capsys):
    """An unknown front and a non-integer preference are both refused by name,
    and neither appends a line."""
    assert fronts.front_prefer_main("ghost-front", "3") == 1
    assert "ghost-front" in capsys.readouterr().err
    add_ok(write_brief(env, "pref2"))
    capsys.readouterr()
    before = len(store.read_ledger(paths.front_record_path("pref2")))
    assert fronts.front_prefer_main("pref2", "high") == 1
    assert "prefer" in capsys.readouterr().err
    assert len(store.read_ledger(paths.front_record_path("pref2"))) == before


def test_close_appends_done(env, capsys):
    """Closing grows the ledger by one line and the fold reads done; closing
    what was never added names the front instead. As with prefer, a rewrite
    of the earlier lines fails here through the byte prefix."""
    add_ok(write_brief(env, "closing"))
    capsys.readouterr()
    before_bytes = paths.front_record_path("closing").read_bytes()
    before = store.read_ledger(paths.front_record_path("closing"))
    assert fronts.front_close_main("closing") == 0
    assert capsys.readouterr().out.strip() == "closing closed"
    after_bytes = paths.front_record_path("closing").read_bytes()
    assert after_bytes.startswith(before_bytes)
    assert len(after_bytes) > len(before_bytes)
    after = store.read_ledger(paths.front_record_path("closing"))
    assert len(after) == len(before) + 1
    folded = store.fold_by_id(after)[0]
    assert folded["state"] == "done"
    assert folded["id"] == before[0]["id"]
    assert fronts.front_close_main("ghost-front") == 1
    assert "ghost-front" in capsys.readouterr().err


def test_front_list_names_state_preference_tasks_and_waits(env, capsys,
                                                           monkeypatch):
    """One line per front: name, state, preference, ready/total tasks, and
    what it waits for. A miscounted task or a lost preference fails here."""
    monkeypatch.chdir(ROOT)
    add_ok(write_brief(env, "runtime"))
    capsys.readouterr()
    assert cli.main(["front", "add", "briefs/panel"]) == 0
    capsys.readouterr()
    assert cli.main(["front", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "panel \u2014 queued \u00b7 prefer 5 \u00b7 tasks 1/4 ready "
        "\u00b7 waits for nothing",
        "runtime \u2014 queued \u00b7 prefer 0 \u00b7 tasks 1/2 ready "
        "\u00b7 waits for nothing",
    ]


def _grant(front: str, role: str, released: bool) -> dict:
    return {"id": ids.mint("slot"), "pool": role, "front": front, "role": role,
            "job": "job-1", "session": "ses-1",
            "granted_at": "2026-09-08T11:52:00+00:00",
            "released_at": "2026-09-08T11:58:00+00:00" if released else None}


def test_status_shows_done_when_and_allocation(env, capsys, monkeypatch):
    """Working shows the front's done-when under its name and held/ceiling per
    role. Held folds last-wins per grant id: an open grant superseded by its
    own released copy counts 0, so that pair plus one separate open grant
    reads 1. Counting ledger lines instead of folded grants reads 2 here."""
    monkeypatch.chdir(ROOT)
    assert cli.main(["front", "add", "briefs/panel"]) == 0
    capsys.readouterr()
    superseded = _grant("panel", "muse", False)
    store.append_ledger(paths.slots_path(), superseded)
    store.append_ledger(paths.slots_path(),
                        {**superseded,
                         "released_at": "2026-09-08T11:59:00+00:00"})
    store.append_ledger(paths.slots_path(), _grant("panel", "muse", False))
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Super+M shows the \u00a713 blocks live from a running collector" \
        in out
    assert "allocation: astra 0/1, grok 0/0, muse 1/3, opus 0/1" in out


def test_worker_may_not_add_a_front(env, capsys, monkeypatch):
    """Front verbs sit on the owner's row of the verb table: a worker session
    is refused by role name."""
    store.write_snapshot(paths.roster_path(), {"sessions": {
        "ses-wrk0001": entities.Session(
            id="ses-wrk0001", role="worker", pool="muse", model="muse",
            pid=1000, launched_by="ses-root",
            started_at="2026-09-08T00:00:00+00:00",
            state="running").to_dict()}})
    monkeypatch.setenv(SESSION_ENV, "ses-wrk0001")
    rc = fronts.front_add_main(str(write_brief(env, "owned")))
    _, err = capsys.readouterr()
    assert rc == 1
    assert "'worker'" in err
    assert not paths.front_record_path("owned").exists()


def test_a_front_that_already_ran_is_added_closed_with_no_tasks(env, capsys):
    """`front add --closed` records history without claiming work.

    The queue's `after` names fronts by their ledger record, and this swarm
    ran two fronts to completion before `front add` existed — so the next
    brief could not wait on the front it really came after. Writing their
    tasks out would claim states no evidence supports; the record alone
    claims only that the front ran and is done.
    """
    brief = write_brief(env, "history")
    assert cli.main(["front", "add", str(brief), "--closed"]) == 0
    capsys.readouterr()
    record = fronts.read_front_record("history")
    assert record["state"] == "done"
    assert store.read_ledger(paths.front_tasks_path("history")) == []
    assert cli.main(["front", "list"]) == 0
    assert "history — done" in capsys.readouterr().out


def test_a_closed_front_is_not_held_to_a_brief_it_cannot_fix(env, capsys):
    """A brief that already ran cannot be corrected after the fact.

    The seed briefs on main predate the validator that shipped after them:
    one has a two-sentence done-when, which a live front is rightly refused
    for. A closed front writes no tasks and spends no allocation, so what
    still has to hold is that it is named once and waits on fronts that
    exist — and that is what is checked.
    """
    text = VALID_BRIEF.format(name="old").replace(
        'done-when = "', 'done-when = "Two things. And another. ')
    brief = write_brief(env, "old", text=text)
    assert cli.main(["front", "add", str(brief)]) == 1
    assert "one sentence" in capsys.readouterr().err
    assert cli.main(["front", "add", str(brief), "--closed"]) == 0
    capsys.readouterr()
    assert fronts.read_front_record("old")["state"] == "done"


def test_a_closed_front_still_must_be_named_once(env, capsys):
    """History is recorded once: a second closed add is refused by name."""
    brief = write_brief(env, "twice")
    assert cli.main(["front", "add", str(brief), "--closed"]) == 0
    capsys.readouterr()
    assert cli.main(["front", "add", str(brief), "--closed"]) == 1
    assert "already on the ledger" in capsys.readouterr().err
