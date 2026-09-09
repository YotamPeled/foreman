"""Launches that fail by name, supervisors that write rules, commissioned tasks.

Every test drives the real entry points against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``. Windowed launches only ever
dry-run: nothing here opens a real window. The break each test catches is
a launch that hangs thirty seconds with nowhere to put a window instead
of refusing by name, a supervisor whose ruling on its own front is
refused or never injected, commissioned work with nowhere to hang, and a
front marked done while tasks or monitors are still open.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from foreman import cli, entities, fronts, paths, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV

SUP = "ses-sup0001"
OTHER_SUP = "ses-sup0002"
FOREMAN_SES = "ses-for0001"
WORKER = "ses-wrk0001"

SCOPE = """WHAT: commissioned work the brief did not name.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""

BRIEF = """name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"

[[task]]
title = "first work"
scope = \"\"\"
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
\"\"\"
verify = "make check first"
size = 1
after = []

[[task]]
title = "second work"
scope = \"\"\"
WHAT: do the second thing.
INPUTS: the first artifact.
OUTPUTS: the second artifact.
OUT OF SCOPE: everything else.
\"\"\"
verify = "make check second"
size = 2
after = ["first work"]

[[monitor]]
question = "how much is done?"
measure = "count-things"
unit = "things"
every = "landing"
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


def write_brief(root: Path, name: str) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(BRIEF.format(name=name),
                                          encoding="utf-8")
    return brief_dir


def add_front_env(root: Path, name: str = "alpha") -> None:
    assert fronts.front_add_main(str(write_brief(root, name))) == 0


def seat_roster() -> None:
    def session(sid: str, role: str,
                front: str | None = None) -> dict:
        return entities.Session(
            id=sid, role=role, pool="opus", model="opus", front=front,
            state="running").to_dict()

    store.write_snapshot(paths.roster_path(), {"sessions": {
        SUP: session(SUP, "supervisor", "alpha"),
        OTHER_SUP: session(OTHER_SUP, "supervisor", "other"),
        FOREMAN_SES: session(FOREMAN_SES, "foreman"),
        WORKER: session(WORKER, "muse", "alpha"),
    }})


def run(monkeypatch, argv: list[str], session: str | None = None) -> int:
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def tasks_of(front: str = "alpha") -> list[dict]:
    return store.fold_by_id(store.read_ledger(paths.front_tasks_path(front)))


def write_config(root: Path, text: str) -> None:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "foreman.toml").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# A windowed launch with no --workspace
# --------------------------------------------------------------------------


def test_no_workspace_takes_the_configured_default(
        env, monkeypatch, capsys):
    """No --workspace lands on default_workspace: the window opens there.

    A launch that waited thirty seconds for a pid file no window would
    write fails here with the launcher's hang instead of this line.
    """
    add_front_env(env)
    write_config(env, '[launch]\ndefault_workspace = "8"\n')
    capsys.readouterr()
    assert run(monkeypatch, ["launch", "supervisor", "alpha",
                             "--repo", str(env), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "workspace 8" in out
    assert "AGENT_WS=8" in out


def test_no_workspace_takes_a_top_level_default(env, monkeypatch, capsys):
    """The default reads from the top level too, as an integer."""
    add_front_env(env)
    write_config(env, 'default_workspace = 7\n')
    capsys.readouterr()
    assert run(monkeypatch, ["launch", "supervisor", "alpha",
                             "--repo", str(env), "--dry-run"]) == 0
    assert "workspace 7" in capsys.readouterr().out


def test_explicit_workspace_wins_over_the_default(
        env, monkeypatch, capsys):
    add_front_env(env)
    write_config(env, '[launch]\ndefault_workspace = "8"\n')
    capsys.readouterr()
    assert run(monkeypatch, ["launch", "supervisor", "alpha",
                             "--workspace", "6", "--repo", str(env),
                             "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "workspace 6" in out
    assert "workspace 8" not in out


def test_desk_and_foreman_launches_take_the_default_too(
        env, monkeypatch, capsys):
    """All three windowed shapes resolve the default, not just supervisors."""
    add_front_env(env)
    write_config(env, '[launch]\ndefault_workspace = "8"\n')
    capsys.readouterr()
    assert run(monkeypatch, ["launch", "merge-desk",
                             "--repo", str(env), "--dry-run"]) == 0
    assert "workspace 8" in capsys.readouterr().out
    assert run(monkeypatch, ["launch", "foreman",
                             "--repo", str(env), "--dry-run"]) == 0
    assert "workspace 8" in capsys.readouterr().out


def test_no_workspace_and_no_default_is_refused_fast_writing_nothing(
        env, monkeypatch, capsys):
    """Refused by name before anything is written, in under a second.

    A launch that spent thirty seconds discovering it had nowhere to
    put a window fails the clock here; one that minted a session first
    fails the roster below.

    `default_workspace` is required configuration and the packaged
    defaults ship one, so reaching this refusal means an install whose
    shipped configuration is gone as well as a user file that names none
    — the only way left to have nowhere to put a window.
    """
    add_front_env(env)
    monkeypatch.setattr(launch_module, "_packaged_defaults", dict)
    capsys.readouterr()
    started = time.monotonic()
    rc = run(monkeypatch, ["launch", "supervisor", "alpha",
                           "--repo", str(env), "--dry-run"])
    elapsed = time.monotonic() - started
    out, err = capsys.readouterr()
    assert rc == 1
    assert "'--workspace'" in err
    assert "default_workspace" in err
    assert elapsed < 10, f"refusal took {elapsed:.1f}s"
    assert out == ""
    assert store.read_snapshot(paths.roster_path(),
                               default={"sessions": {}}) == {"sessions": {}}
    assert not paths.sessions_dir().exists()


def test_malformed_default_workspace_is_refused_by_name(
        env, monkeypatch, capsys):
    """A default that is not digits names itself, not the flag."""
    add_front_env(env)
    write_config(env, '[launch]\ndefault_workspace = "six"\n')
    capsys.readouterr()
    assert run(monkeypatch, ["launch", "supervisor", "alpha",
                             "--repo", str(env), "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "default_workspace" in err
    assert "six" in err


# --------------------------------------------------------------------------
# foreman rule <front> from the front's own supervisor
# --------------------------------------------------------------------------


def test_supervisor_writes_a_rule_on_its_own_front(
        env, monkeypatch, capsys):
    """Accepted, recorded with a supervisor source, injected later."""
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["rule", "alpha",
                             "Verify by re-running; nothing else counts."],
               session=SUP) == 0
    rid = capsys.readouterr().out.strip()
    records = store.read_ledger(paths.rulings_path())
    assert len(records) == 1
    assert records[0]["id"] == rid
    assert records[0]["scope"] == "alpha"
    assert records[0]["source"] == "supervisor"
    assert records[0]["text"] == "Verify by re-running; nothing else counts."

    # The ruling travels into later launches on that front like any other.
    # The seated supervisor would block the summon (one front, one
    # supervisor), and its ledger assertions are done, so stand it down.
    store.write_snapshot(paths.roster_path(), {"sessions": {}})
    capsys.readouterr()
    assert run(monkeypatch, ["launch", "supervisor", "alpha",
                             "--workspace", "6", "--repo", str(env),
                             "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert ("- from the rulings ledger: "
            "Verify by re-running; nothing else counts.") in out


def test_supervisor_is_still_refused_a_swarm_rule(
        env, monkeypatch, capsys):
    """Swarm rules stay foreman-only; the refusal names the role."""
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["rule", "swarm", "Everyone verifies."],
               session=SUP) == 1
    err = capsys.readouterr().err
    assert "'supervisor'" in err
    assert "'rule'" in err
    assert store.read_ledger(paths.rulings_path()) == []


def test_supervisor_is_refused_a_rule_on_another_front(
        env, monkeypatch, capsys):
    add_front_env(env)
    add_front_env(env, "other")
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["rule", "other", "Not my front."],
               session=SUP) == 1
    err = capsys.readouterr().err
    assert SUP in err
    assert "'alpha'" in err
    assert "'other'" in err
    assert store.read_ledger(paths.rulings_path()) == []


def test_foreman_still_writes_swarm_and_front_rules(
        env, monkeypatch, capsys):
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["rule", "swarm", "Everyone verifies."],
               session=FOREMAN_SES) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["rule", "alpha", "Front ruling."],
               session=FOREMAN_SES) == 0
    capsys.readouterr()
    records = store.read_ledger(paths.rulings_path())
    assert [(record["scope"], record["source"]) for record in records] == \
        [("swarm", "foreman"), ("alpha", "foreman")]


def test_worker_is_refused_a_front_rule(env, monkeypatch, capsys):
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["rule", "alpha", "Workers rule nothing."],
               session=WORKER) == 1
    assert "'rule'" in capsys.readouterr().err
    assert store.read_ledger(paths.rulings_path()) == []


# --------------------------------------------------------------------------
# foreman task add <front>: commissioned work hangs under a task
# --------------------------------------------------------------------------


def test_supervisor_commissions_a_task(env, monkeypatch, capsys):
    """Appended ready, with its contract intact and the screen's mark."""
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["task", "add", "alpha", "--title", "extra work",
                             "--scope", SCOPE, "--verify", "make check extra",
                             "--size", "2"], session=SUP) == 0
    tid = capsys.readouterr().out.strip().split()[0]
    tasks = {task["title"]: task for task in tasks_of()}
    assert set(tasks) == {"first work", "second work", "extra work"}
    added = tasks["extra work"]
    assert added["id"] == tid
    assert added["front"] == "alpha"
    assert added["state"] == "ready"
    assert (added["units_done"], added["units_total"]) == (0, 2)
    assert added["scope"] == SCOPE.strip()
    assert added["verify"] == "make check extra"
    assert added["after"] == []
    assert added["added_by"] == "supervisor"
    # The brief's own tasks carry no mark.
    assert tasks["first work"].get("added_by", "") == ""

    capsys.readouterr()
    assert run(monkeypatch, ["status"], session=SUP) == 0
    out = capsys.readouterr().out
    assert "extra work" in out
    assert "added by supervisor" in out


def test_commissioned_task_behind_a_predecessor_waits(
        env, monkeypatch, capsys):
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["task", "add", "alpha", "--title", "later work",
                             "--scope", SCOPE, "--verify", "make check later",
                             "--size", "1", "--after", "first work"],
               session=SUP) == 0
    capsys.readouterr()
    tasks = {task["title"]: task for task in tasks_of()}
    assert tasks["later work"]["state"] == "waiting"
    assert tasks["later work"]["after"] == ["first work"]


def test_commissioned_task_refusals_name_every_field(
        env, monkeypatch, capsys):
    """Wrong four ways, refused once: scope, verify, size and after."""
    add_front_env(env)
    seat_roster()
    before = tasks_of()
    capsys.readouterr()
    assert run(monkeypatch, ["task", "add", "alpha", "--title", "bad work",
                             "--scope", "WHAT: no other headings.",
                             "--verify", "", "--size", "0",
                             "--after", "no such task"], session=SUP) == 1
    err = capsys.readouterr().err
    assert err.count("refused:") == 1
    for marker in ("OUT OF SCOPE", "'verify'", "'size'",
                   "'after'", "no such task"):
        assert marker in err, f"{marker!r} not named in: {err.strip()}"
    assert tasks_of() == before


def test_commissioned_task_reuses_a_title_is_refused(
        env, monkeypatch, capsys):
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["task", "add", "alpha", "--title", "first work",
                             "--scope", SCOPE, "--verify", "make check",
                             "--size", "1"], session=SUP) == 1
    err = capsys.readouterr().err
    assert "first work" in err
    assert "unique" in err


def test_task_add_is_refused_beyond_the_front_supervisor(
        env, monkeypatch, capsys):
    """Another front's supervisor, a worker and the foreman are all refused;
    the owner is never refused."""
    add_front_env(env)
    seat_roster()
    argv = ["task", "add", "alpha", "--title", "extra work",
            "--scope", SCOPE, "--verify", "make check extra", "--size", "1"]
    capsys.readouterr()
    assert run(monkeypatch, argv, session=OTHER_SUP) == 1
    err = capsys.readouterr().err
    assert "'alpha'" in err
    assert run(monkeypatch, argv, session=WORKER) == 1
    assert "'task add'" in capsys.readouterr().err
    assert run(monkeypatch, argv, session=FOREMAN_SES) == 1
    assert "'task add'" in capsys.readouterr().err
    assert run(monkeypatch, argv, session=None) == 0
    capsys.readouterr()
    tasks = {task["title"]: task for task in tasks_of()}
    assert tasks["extra work"]["added_by"] == "owner"


def test_task_add_on_an_unknown_front_is_refused(
        env, monkeypatch, capsys):
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["task", "add", "ghost", "--title", "extra work",
                             "--scope", SCOPE, "--verify", "make check",
                             "--size", "1"], session=SUP) == 1
    assert "ghost" in capsys.readouterr().err


# --------------------------------------------------------------------------
# foreman front done <front>: the supervisor closes a finished front
# --------------------------------------------------------------------------


def land_everything(monkeypatch) -> None:
    """Verify-by-hand every task on alpha and land it, then measure."""
    assert run(monkeypatch, ["evidence", "--on", "alpha",
                             "--claim", "ran every check by hand",
                             "--status", "CONFIRMED",
                             "--command", "make check"], session=SUP) == 0
    for title in ("first work", "second work", "extra work"):
        titles = {task["title"] for task in tasks_of()}
        if title not in titles:
            continue
        assert run(monkeypatch, ["task", "built", title,
                                 "--did-myself", "Ran it by hand."],
                   session=SUP) == 0
        assert run(monkeypatch, ["task", "landed", title,
                                 "--head", "abc123"], session=SUP) == 0
    assert run(monkeypatch, ["measure", "alpha", "count-things",
                             "--value", "8", "--of", "8",
                             "--command", "count-things",
                             "--output", "8"], session=SUP) == 0


def test_front_done_refused_naming_tasks_and_monitors(
        env, monkeypatch, capsys):
    """Nothing landed, nothing measured: the refusal names both lists."""
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["front", "done", "alpha"], session=SUP) == 1
    err = capsys.readouterr().err
    assert "first work" in err
    assert "second work" in err
    assert "how much is done?" in err
    record = fronts.read_front_record("alpha")
    assert record is not None and record["state"] != "done"


def test_front_done_accepted_once_finished(env, monkeypatch, capsys):
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["task", "add", "alpha", "--title", "extra work",
                             "--scope", SCOPE, "--verify", "make check extra",
                             "--size", "1"], session=SUP) == 0
    capsys.readouterr()
    land_everything(monkeypatch)
    capsys.readouterr()
    assert run(monkeypatch, ["front", "done", "alpha"], session=SUP) == 0
    assert capsys.readouterr().out.strip() == "alpha done"
    record = fronts.read_front_record("alpha")
    assert record is not None and record["state"] == "done"
    # The revised copy keeps the brief's monitors.
    assert record["monitors"][0]["measure"] == "count-things"


def test_front_done_still_waits_on_an_unmeasured_monitor(
        env, monkeypatch, capsys):
    """Every task landed but the monitor never measured: refused by name."""
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["evidence", "--on", "alpha",
                             "--claim", "ran every check by hand",
                             "--status", "CONFIRMED",
                             "--command", "make check"], session=SUP) == 0
    for title in ("first work", "second work"):
        assert run(monkeypatch, ["task", "built", title,
                                 "--did-myself", "Ran it by hand."],
                   session=SUP) == 0
        assert run(monkeypatch, ["task", "landed", title,
                                 "--head", "abc123"], session=SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["front", "done", "alpha"], session=SUP) == 1
    err = capsys.readouterr().err
    assert "how much is done?" in err
    assert "not landed" not in err


def test_front_done_is_refused_beyond_the_front_supervisor(
        env, monkeypatch, capsys):
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["front", "done", "alpha"],
               session=WORKER) == 1
    assert "'front done'" in capsys.readouterr().err
    assert run(monkeypatch, ["front", "done", "alpha"],
               session=FOREMAN_SES) == 1
    assert "'front done'" in capsys.readouterr().err
    assert run(monkeypatch, ["front", "done", "alpha"],
               session=OTHER_SUP) == 1
    assert "'alpha'" in capsys.readouterr().err
    # The owner is never refused, but the gate still holds: unfinished
    # work is named, not closed.
    assert run(monkeypatch, ["front", "done", "alpha"],
               session=None) == 1
    assert "first work" in capsys.readouterr().err


def test_front_done_on_an_unknown_front_is_refused(
        env, monkeypatch, capsys):
    add_front_env(env)
    seat_roster()
    capsys.readouterr()
    assert run(monkeypatch, ["front", "done", "ghost"], session=SUP) == 1
    assert "ghost" in capsys.readouterr().err


def test_the_packaged_defaults_ship_a_workspace(env, monkeypatch, capsys):
    """A machine nobody configured still has somewhere to put a window.

    `default_workspace` is required configuration (owner ruling), so
    Foreman ships one: a fresh install launches, and the collector's
    automatic relaunch — which can name no workspace of its own — has a
    value to fall back on.
    """
    add_front_env(env)
    capsys.readouterr()
    assert launch_module.configured_default_workspace() == "6"
    assert run(monkeypatch, ["launch", "supervisor", "alpha",
                             "--repo", str(env), "--dry-run"]) == 0
    assert "workspace 6" in capsys.readouterr().out


def test_the_owners_file_beats_the_packaged_workspace(env, monkeypatch,
                                                      capsys):
    """A value the owner wrote wins, at either level of the file."""
    add_front_env(env)
    write_config(env, 'default_workspace = 7\n')
    capsys.readouterr()
    assert launch_module.configured_default_workspace() == "7"
    write_config(env, '[launch]\ndefault_workspace = "9"\n')
    assert launch_module.configured_default_workspace() == "9"


def test_doctor_names_a_configuration_with_no_workspace(env, monkeypatch,
                                                        capsys):
    """Required configuration is reported when it is missing."""
    from foreman import doctor as doctor_module

    shipped = launch_module._packaged_defaults
    monkeypatch.setattr(launch_module, "_packaged_defaults", dict)
    # Through `foreman doctor` itself, not the check alone: a check the
    # report never calls is a check that does not exist, and asserting on
    # the function directly cannot tell the difference.
    capsys.readouterr()
    assert doctor_module.doctor_main() == 1
    out = capsys.readouterr().out
    assert "default_workspace" in out
    assert "fix: add a `[launch]` table" in out

    # Restore by name, never monkeypatch.undo(): undo reverts the env
    # fixture's FOREMAN_STATE as well, and doctor would then read the
    # machine's real state directory.
    monkeypatch.setattr(launch_module, "_packaged_defaults", shipped)
    capsys.readouterr()
    assert doctor_module.doctor_main() == 0
    assert "default_workspace" not in capsys.readouterr().out
