"""doctor, hooks, migrations.

Every test drives the real verbs against a fresh FOREMAN_STATE and
FOREMAN_CONFIG under tmp_path. Expected strings are hand-derived
literals, never built with the modules' own helpers.

The break each group catches is in its docstring.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from foreman import caller, cli, hooks, paths, procs, store
from foreman.caller import SESSION_ENV

FIXTURES = Path(__file__).with_name("fixture-hooks")
FAIL_HOOK = FIXTURES / "fail.sh"
RECORD_HOOK = FIXTURES / "record.sh"

DEAD_PID = 999999
MIGRATION = "1788902590"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


def run(argv, monkeypatch, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def roster(sessions: dict) -> None:
    store.write_snapshot(paths.roster_path(), {"sessions": sessions})


def session(sid, role, pid=None, **extra):
    record = {"id": sid, "role": role, "pool": "muse", "model": "muse-test",
              "front": "fx", "job": None, "pid": pid, "pgid": pid,
              "pid_starttime": None, "worktree": "", "log": "",
              "timeout": "20m", "state": "running"}
    record.update(extra)
    return record


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def test_doctor_clean_on_empty_state(env, monkeypatch, capsys):
    """An untouched state directory agrees with itself.

    Fails on a check that fires with nothing seeded (a missing file read
    as a divergence) or on a verb that cannot run as the owner.
    """
    assert run(["doctor"], monkeypatch) == 0
    assert capsys.readouterr().out == "doctor: clean\n"


def test_doctor_refuses_a_worker(env, monkeypatch, capsys):
    """Doctor is an owner/foreman/supervisor verb, like status.

    Fails on a gate that reads as open to every role: the MCP server
    would then offer diagnostics to sessions that hold no verb.
    """
    roster({"ses-wrk": session("ses-wrk", "muse", pid=os.getpid())})
    assert run(["doctor"], monkeypatch, session="ses-wrk") == 1
    assert "may not call 'doctor'" in capsys.readouterr().err


def seed_divergences() -> dict:
    """Nine real divergences across six checks; returns what to assert."""
    live_start = procs.proc_starttime(os.getpid())
    assert live_start is not None
    roster({
        "ses-dead01": session("ses-dead01", "muse", pid=DEAD_PID,
                              pid_starttime=12345, job="job-1"),
        "ses-dead02": session("ses-dead02", "supervisor", pid=DEAD_PID,
                              pid_starttime=12345),
        "ses-reused": session("ses-reused", "muse", pid=os.getpid(),
                              pid_starttime=live_start + 10**9,
                              job="job-2"),
        "ses-exited": session("ses-exited", "muse", pid=DEAD_PID,
                              state="exited"),
    })
    (paths.state_dir() / "worktrees" / "orphan-dir").mkdir(parents=True)
    gone = str(paths.state_dir() / "worktrees" / "gone-wt")
    write(paths.front_jobs_path("fx"),
          {"id": "job-1", "state": "running", "worktree": gone})
    write(paths.front_jobs_path("fx"),
          {"id": "job-2", "state": "running", "worktree": gone})
    write(paths.front_record_path("fx"),
          {"id": "front-1", "name": "fx", "state": "active"})
    write(paths.slots_path(),
          {"id": "slot-1", "pool": "muse", "front": "fx", "role": "muse",
           "job": "job-1", "session": "ses-exited",
           "granted_at": "2026-09-08T00:00:00+00:00",
           "released_at": None, "released_because": ""})
    write(paths.slots_path(),
          {"id": "slot-2", "pool": "muse", "front": "fx", "role": "muse",
           "job": "job-9", "session": "ses-ghost",
           "granted_at": "2026-09-08T00:00:00+00:00",
           "released_at": None, "released_because": ""})
    config = paths.config_file()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[pool.ghost]\ncap = 2\n", encoding="utf-8")
    store.write_snapshot(paths.collector_path(),
                         {"code_head": "0" * 40, "code_mtime": 0,
                          "sessions": {}, "relaunches": [], "parents": {}})


def test_doctor_names_each_divergence_with_its_fix(
        env, monkeypatch, capsys):
    """Every seeded divergence prints with the command that fixes it.

    Fails on a check that misses its divergence, on a fix line naming a
    command that does not exist, and on exit 0 with problems outstanding
    (doctor must read as a shell condition).
    """
    seed_divergences()
    assert run(["doctor"], monkeypatch) == 1
    out = capsys.readouterr().out
    expected = [
        # dead worker session, with the job it ran
        ("stale session ses-dead01 (role muse): process 999999 is gone",
         "fix: foreman job fail job-1 "
         "--finding 'session ses-dead01 is gone'"),
        # dead supervisor: a person relaunches from the checkpoint
        ("stale session ses-dead02 (role supervisor): process 999999 is gone",
         "fix: foreman relaunch ses-dead02"),
        # the pid is alive but the start time moved: pid reuse, not life
        ("stale session ses-reused (role muse): "
         "pid %d is now a different process" % os.getpid(),
         "fix: foreman job fail job-2"),
        # a worktree no job owns, and a live job whose worktree is gone
        ("orphan worktree", "fix: git worktree remove --force"),
        ("job job-1 on front 'fx' is 'running' but its worktree",
         "fix: foreman job fail job-1"),
        # open grants held by an exited session and by no session at all
        ("open slot grant slot-1", "fix: foreman collector once"),
        ("open slot grant slot-2", "fix: foreman collector once"),
        # a cap on a pool nothing registers governs nothing
        ("caps pool 'ghost' that no adapter registers",
         "fix: remove the [pool.'ghost'] table"),
        # the collector predates this checkout
        ("collector is running code older than this checkout",
         "fix: foreman collector restart"),
        # a front line from before the merge mode existed
        ("front 'fx' has no merge mode",
         "fix: foreman migrate"),
    ]
    for divergence, fix in expected:
        assert divergence in out, divergence
        assert fix in out, fix
    assert out.rstrip().endswith("doctor: 11 problem(s)")


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------


def install(monkeypatch, event: str, fixture: Path) -> int:
    return run(["hook", "install", event, str(fixture)], monkeypatch)


def test_hook_install_and_list(env, monkeypatch, capsys):
    """Hooks install executable and list what would fire.

    Fails on an install that forgets the exec bit, on an unknown event
    accepted silently, and on a list that hides an installed hook.
    """
    assert install(monkeypatch, "on-return", FAIL_HOOK) == 0
    capsys.readouterr()
    assert install(monkeypatch, "on-return", RECORD_HOOK) == 0
    capsys.readouterr()
    assert run(["hook", "list"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "on-return: fail.sh" in out
    assert "on-return: record.sh" in out
    assert run(["hook", "install", "on-party", str(FAIL_HOOK)],
               monkeypatch) == 1
    assert "unknown hook event 'on-party'" in capsys.readouterr().err
    assert install(monkeypatch, "on-return", FAIL_HOOK) == 1
    assert "already exists" in capsys.readouterr().err


def test_failing_hook_does_not_stop_the_run(env, monkeypatch, capsys):
    """One bad hook never stops the swarm: failures print, the run goes on.

    Fails on a hook failure that raises, on hooks running out of order,
    and on hooks firing from anywhere but the user's config directory.
    """
    record_file = env / "record.jsonl"
    monkeypatch.setenv("HOOK_RECORD_FILE", str(record_file))
    assert install(monkeypatch, "on-alert", FAIL_HOOK) == 0
    assert install(monkeypatch, "on-alert", RECORD_HOOK) == 0
    capsys.readouterr()
    results = hooks.fire("on-alert", {"kind": "quota", "subject": "muse"})
    assert results == [("fail.sh", 1), ("record.sh", 0)]
    event = json.loads(record_file.read_text(encoding="utf-8"))
    assert event["event"] == "on-alert"
    assert event["kind"] == "quota"
    assert "failed (exit 1); continuing" in capsys.readouterr().err

    # *.sample never runs, and neither does a hook planted anywhere but
    # the user's own config directory.
    planted = paths.state_dir() / "hooks" / "on-alert" / "evil.sh"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("#!/bin/sh\necho planted >> '%s'\n" % record_file,
                       encoding="utf-8")
    planted.chmod(0o755)
    sample = hooks.hooks_dir("on-alert") / "zap.sample"
    sample.write_text("#!/bin/sh\necho sampled >> '%s'\n" % record_file,
                      encoding="utf-8")
    sample.chmod(0o755)
    before = record_file.read_text(encoding="utf-8")
    hooks.fire("on-alert", {"kind": "quota", "subject": "muse"})
    after = record_file.read_text(encoding="utf-8")
    assert after.count("\n") == before.count("\n") + 1

    # No hooks directory at all is silence, not an error.
    assert hooks.fire("on-land", {"task": "task-1"}) == []


def test_hook_fires_on_landing(env, monkeypatch, capsys):
    """The landing verb fires on-land after the ledger write is durable.

    Fails on a landing no hook observes, and on a hook that breaks the
    verb's own exit code.
    """
    record_file = env / "land.jsonl"
    monkeypatch.setenv("HOOK_RECORD_FILE", str(record_file))
    assert install(monkeypatch, "on-land", RECORD_HOOK) == 0
    capsys.readouterr()
    write(paths.front_record_path("fx"),
          {"id": "front-1", "name": "fx", "state": "active",
           "allocation": {}, "merge": "self"})
    write(paths.front_tasks_path("fx"),
          {"id": "task-1", "front": "fx", "title": "First",
           "state": "built"})
    assert run(["task", "landed", "task-1", "--head", "deadbeef"],
               monkeypatch) == 0
    capsys.readouterr()
    event = json.loads(record_file.read_text(encoding="utf-8"))
    assert event["event"] == "on-land"
    assert event["task"] == "task-1"
    assert event["front"] == "fx"
    assert event["head"] == "deadbeef"


def test_freeze_and_thaw_fire_on_freeze(env, monkeypatch, capsys):
    """The freeze transition fires on-freeze; repeating it is refused.

    Fails on a freeze nobody observes and on a second freeze that
    silently re-freezes (or a thaw that silently unthaws nothing).
    """
    record_file = env / "freeze.jsonl"
    monkeypatch.setenv("HOOK_RECORD_FILE", str(record_file))
    assert install(monkeypatch, "on-freeze", RECORD_HOOK) == 0
    capsys.readouterr()
    assert run(["freeze"], monkeypatch) == 0
    capsys.readouterr()
    assert paths.frozen_path().exists()
    assert run(["freeze"], monkeypatch) == 1
    assert "already frozen" in capsys.readouterr().err
    assert run(["thaw"], monkeypatch) == 0
    capsys.readouterr()
    assert not paths.frozen_path().exists()
    assert run(["thaw"], monkeypatch) == 1
    assert "not frozen" in capsys.readouterr().err
    events = [json.loads(line) for line in
              record_file.read_text(encoding="utf-8").splitlines()]
    assert [(e["event"], e["frozen"]) for e in events] == [
        ("on-freeze", True), ("on-freeze", False)]


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------


def test_migrate_pending_flag(env, monkeypatch, capsys):
    """--pending is a shell condition: 0 when work waits, 1 when none.

    Fails on a runner that cannot see its own packaged migration or
    that marks work done without running it.
    """
    assert run(["migrate", "--pending"], monkeypatch) == 0
    assert MIGRATION in capsys.readouterr().out
    assert run(["migrate"], monkeypatch) == 0
    assert (paths.state_dir() / "migrations" / MIGRATION).exists()
    assert run(["migrate", "--pending"], monkeypatch) == 1
    assert "nothing pending" in capsys.readouterr().out


def seed_v1_state() -> dict[str, str]:
    """A v1 state directory: front lines without merge, merges without front."""
    write(paths.front_record_path("alpha"),
          {"id": "front-1", "name": "alpha", "state": "active",
           "allocation": {"muse": 2}})
    write(paths.front_tasks_path("alpha"),
          {"id": "task-1", "front": "alpha", "title": "First",
           "state": "built"})
    write(paths.merges_path(),
          {"id": "merge-1", "branch": "v2/alpha", "tasks": ["task-1"],
           "target": "main", "result": "requested",
           "requested_at": "2026-09-08T00:00:00+00:00"})
    write(paths.merges_path(),
          {"id": "merge-2", "branch": "v2/orphan", "tasks": ["task-9"],
           "target": "main", "result": "requested",
           "requested_at": "2026-09-08T00:00:00+00:00"})
    return {"front": str(paths.front_record_path("alpha")),
            "merges": str(paths.merges_path())}


def ledger_lines(path: str) -> list[dict]:
    return store.read_ledger(Path(path))


def test_migrate_moves_a_v1_state_directory_forward(
        env, monkeypatch, capsys):
    """The first migration backfills what v2 added, once, then stands down.

    Fails on a front left without its merge mode, on a merge whose front
    the tasks name left blank, on an unrepairable merge blocking the
    queue, and on a second run that writes anything at all.
    """
    ledgers = seed_v1_state()
    assert run(["migrate"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "gains merge 'self'" in out
    assert "merge 'merge-1' gains front 'alpha'" in out
    assert "merge 'merge-2'" in out and "left without one" in out

    fronts = store.fold_by_id(ledger_lines(ledgers["front"]))
    assert fronts[-1]["merge"] == "self"
    assert fronts[-1]["name"] == "alpha"
    assert fronts[-1]["allocation"] == {"muse": 2}
    merges = {row["id"]: row
              for row in store.fold_by_id(ledger_lines(ledgers["merges"]))}
    assert merges["merge-1"]["front"] == "alpha"
    assert merges["merge-2"].get("front") in (None, "")
    assert (paths.state_dir() / "migrations" / MIGRATION).exists()

    # The second run does nothing: marked migrations never re-run, and
    # the guard in the script would make a re-run a no-op regardless.
    before = {key: Path(key).read_bytes() for key in ledgers.values()}
    assert run(["migrate"], monkeypatch) == 0
    assert "nothing pending" in capsys.readouterr().out
    for key, body in before.items():
        assert Path(key).read_bytes() == body

    # Doctor is clean on the state directory the migration just moved.
    assert run(["doctor"], monkeypatch) == 0
    assert capsys.readouterr().out == "doctor: clean\n"


def test_migration_runs_with_no_foreman_on_the_path(env):
    """The migration is a standalone script, never an import.

    Fails on a migration reaching back into the checkout: it runs here
    with an empty import path and a working directory that is not the
    repository, so only the standard library answers.
    """
    seed_v1_state()
    script = (Path(paths.__file__).with_name("migrations")
              / f"{MIGRATION}.py")
    stripped = {"PATH": os.environ.get("PATH", os.defpath)}
    proc = subprocess.run(
        [sys.executable, str(script),
         "--state-dir", str(paths.state_dir()),
         "--config-dir", str(paths.config_dir())],
        capture_output=True, text=True, cwd=str(env), env=stripped)
    assert proc.returncode == 0, proc.stderr
    assert "gains merge 'self'" in proc.stdout
    fronts = store.fold_by_id(
        store.read_ledger(paths.front_record_path("alpha")))
    assert fronts[-1]["merge"] == "self"


def test_callers_without_a_session_are_refused(env, monkeypatch, capsys):
    """Hook, freeze and migrate are owner verbs: unknown sessions refuse.

    Fails on a gate left open, which the MCP server would read as an
    invitation to every role.
    """
    assert run(["hook", "list"], monkeypatch, session="ses-nobody") == 1
    assert "unregistered writer" in capsys.readouterr().err
    assert run(["freeze"], monkeypatch, session="ses-nobody") == 1
    assert run(["migrate"], monkeypatch, session="ses-nobody") == 1
    # The refusal is recorded where the collector looks for it, and the
    # roster it never minted stays empty.
    anomalies = store.read_ledger(paths.anomalies_path())
    assert any(row.get("kind") == "unregistered writer"
               and row.get("subject") == "ses-nobody"
               and row.get("resolved_at") is None for row in anomalies)
    assert caller.read_roster() == {"sessions": {}}


def seed_gone_worktree_job(*, front_state: str, with_task: bool) -> str:
    """A returned job whose worktree directory was never created.

    ``front_state`` is written on the front record; ``with_task`` is
    whether the job's task still sits on the ledger. A closed front's
    job often names a task that is gone; a live front's job still has
    one, which is why today's ``job fail`` can file a finding.
    """
    gone = str(paths.state_dir() / "worktrees" / "gone-wt")
    write(paths.front_record_path("fx"),
          {"id": "front-1", "name": "fx", "state": front_state,
           "merge": "self"})
    write(paths.front_jobs_path("fx"),
          {"id": "job-1", "state": "returned", "worktree": gone,
           "task": "tas-0001"})
    if with_task:
        write(paths.front_tasks_path("fx"),
              {"id": "tas-0001", "front": "fx", "title": "First",
               "state": "landed"})
    return gone


def printed_fix_argv(out: str, divergence: str) -> list[str]:
    """The argv of the ``fix:`` line under ``divergence``, minus ``foreman``.

    The test runs that argv as printed: a reconstructed command would
    not catch a fix line that names a verb that then refuses.
    """
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if divergence not in line:
            continue
        for follow in lines[i + 1:]:
            stripped = follow.strip()
            if stripped.startswith("fix: "):
                argv = shlex.split(stripped[len("fix: "):])
                assert argv and argv[0] == "foreman", stripped
                return argv[1:]
            if stripped.startswith("doctor:"):
                break
    raise AssertionError(f"no fix line under {divergence!r} in:\n{out}")


def folded_job(front: str, job_id: str) -> dict:
    folded = store.fold_by_id(store.read_ledger(paths.front_jobs_path(front)))
    return {row["id"]: row for row in folded}[job_id]


def test_doctor_reports_a_gone_worktree_on_a_done_front_as_history(
        env, monkeypatch, capsys):
    """A returned job on a done front is history, not a live failure.

    Fails if doctor still prints today's live-front line (and today's
    ``job fail`` would then record landed work as failed), if it prints
    no command, or if the printed command refuses a job whose task is
    no longer on the ledger.
    """
    gone = seed_gone_worktree_job(front_state="done", with_task=False)
    assert run(["doctor"], monkeypatch) == 1
    out = capsys.readouterr().out
    divergence = (f"job job-1 on done front 'fx' is history "
                  f"(worktree {gone} is gone)")
    assert divergence in out
    assert "is 'returned' but its worktree" not in out
    argv = printed_fix_argv(out, divergence)
    assert argv == ["job", "fail", "job-1", "--closed"]
    assert run(argv, monkeypatch) == 0
    assert capsys.readouterr().out.strip() == "job-1 history"
    assert folded_job("fx", "job-1")["state"] == "history"
    assert store.read_ledger(paths.front_findings_path("fx")) == []
    assert run(["doctor"], monkeypatch) == 0
    assert capsys.readouterr().out == "doctor: clean\n"


def test_doctor_still_fails_a_gone_worktree_on_a_live_front(
        env, monkeypatch, capsys):
    """A returned job on an open front is the problem it was, and the
    same ``job fail --finding`` still records the failure.

    Fails if a done-front change swallows the live case, or if the
    printed fix no longer exits 0 when pasted.
    """
    gone = seed_gone_worktree_job(front_state="active", with_task=True)
    assert run(["doctor"], monkeypatch) == 1
    out = capsys.readouterr().out
    divergence = (f"job job-1 on front 'fx' is 'returned' "
                  f"but its worktree {gone} is gone")
    assert divergence in out
    assert "is history" not in out
    argv = printed_fix_argv(out, divergence)
    assert argv[:3] == ["job", "fail", "job-1"]
    assert "--closed" not in argv
    assert run(argv, monkeypatch) == 0
    capsys.readouterr()
    assert folded_job("fx", "job-1")["state"] == "failed"
    findings = store.read_ledger(paths.front_findings_path("fx"))
    assert len(findings) == 1
    assert findings[0]["on"] == "tas-0001"
    assert run(["doctor"], monkeypatch) == 0
    assert capsys.readouterr().out == "doctor: clean\n"


def test_job_fail_closed_refuses_a_live_front(env, monkeypatch, capsys):
    """``--closed`` is the history path: a job on an open front stays
    a live failure, and the flag will not re-label it.

    Fails if ``--closed`` accepts a live front (and then writes history
    where a finding belongs, or fails the job with no task line).
    """
    seed_gone_worktree_job(front_state="active", with_task=True)
    assert run(["job", "fail", "job-1", "--closed"], monkeypatch) == 1
    err = capsys.readouterr().err
    assert "not done" in err
    assert folded_job("fx", "job-1")["state"] == "returned"
    assert store.read_ledger(paths.front_findings_path("fx")) == []


def test_job_fail_closed_refuses_an_unknown_job(env, monkeypatch, capsys):
    """An unknown job is still unknown with ``--closed``.

    Fails if the flag skips the lookup and writes a history line for a
    job nobody recorded.
    """
    assert run(["job", "fail", "job-ghost", "--closed"], monkeypatch) == 1
    assert "unknown job 'job-ghost'" in capsys.readouterr().err


def test_doctor_reports_a_dead_merge_desk(env, monkeypatch, capsys):
    """A dead desk is worse than a dead worker and was reported by nobody.

    The roles doctor checked were the job roles and the supervisor, so a
    merge desk whose process is gone passed silently — while it may hold a
    merge it took, which nothing else may take while the record says it is
    being merged. Found by running the version two done-when: the desk was
    dead on the roster and `foreman doctor` said nothing about it.
    """
    roster({"ses-desk01": session("ses-desk01", "merge-desk", pid=DEAD_PID,
                                  pid_starttime=12345)})
    assert run(["doctor"], monkeypatch) == 1
    out = capsys.readouterr().out
    assert ("stale session ses-desk01 (role merge-desk): "
            "process 999999 is gone") in out
    assert "fix: foreman launch merge-desk" in out
