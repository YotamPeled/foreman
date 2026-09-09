"""Units and job outcomes told truthfully: one test per spec item.

Every test drives the real entry points against a fresh FOREMAN_STATE and
FOREMAN_CONFIG directory. Branch-moved checks run against real git
repositories in tmp_path; collector transitions run real ticks with a
scripted pool adapter and real (then killed) processes. No test writes
to a real state directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, paths, procs, store
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.pools import LaunchContext, PoolAdapter

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
WRK = "ses-wrk0001"
TICK_AT = "2026-09-08T12:00:00+00:00"
WRITE_TS = 1757221200.0
WRITE_AT = "2025-09-07T05:00:00+00:00"

FLOW_BRIEF = """name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 2

[[task]]
title = "first"
scope = \"\"\"
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
\"\"\"
verify = "make check first"
size = 3
after = []
"""

SPEC_OK = (
    "WHAT: do the first thing.\n"
    "INPUTS: the brief.\n"
    "OUTPUTS: the artifact.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests -q\n"
)


class FakeAdapter(PoolAdapter):
    """Scripted pool: observe returns the script per session, else idle;
    launch records the pid file and spawns nothing real."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    script: dict[str, dict] = {}

    def observe(self, session) -> dict:
        return dict(self.script.get(session.id or "", {
            "transcript_mtime": None,
            "cpu_s": 0.0,
            "finish_present": False,
            "finish_rc": None,
        }))

    def launch(self, ctx: LaunchContext) -> int:
        pid = os.getpid()
        ctx.pid_path.write_text(f"{pid}\n", encoding="utf-8")
        return pid

    def command_str(self, ctx: LaunchContext) -> str:
        return "fake-exec"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    return tmp_path


@pytest.fixture()
def fake_pool():
    from foreman import pools

    FakeAdapter.script = {}
    pools.register("fake", FakeAdapter())
    try:
        yield FakeAdapter
    finally:
        pools.unregister("fake")


@pytest.fixture()
def children():
    procs_list: list[subprocess.Popen] = []
    try:
        yield procs_list
    finally:
        for proc in procs_list:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def iso(moment: datetime) -> str:
    return moment.isoformat()


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "test")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(path, "add", "seed.txt")
    git(path, "commit", "-qm", "seed")
    return path


def make_moved_repo(path: Path) -> Path:
    """A repo whose job branch holds one commit past main: the work exists."""
    repo = make_repo(path)
    git(repo, "checkout", "-qb", "job-branch")
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    git(repo, "add", "work.txt")
    git(repo, "commit", "-qm", "the work")
    return repo


def write_spec(root: Path, name: str) -> str:
    spec = root / name
    spec.write_text(SPEC_OK, encoding="utf-8")
    return str(spec)


def write_brief(root: Path, name: str) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        FLOW_BRIEF.format(name=name), encoding="utf-8")
    if not (root / ".git").exists():
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@example.invalid")
        git(root, "config", "user.name", "test")
        git(root, "commit", "-q", "--allow-empty", "-m", "root")
    return brief_dir


def add_front(env, monkeypatch, capsys, name="flow"):
    assert run(monkeypatch, ["front", "add", str(write_brief(env, name))]) == 0
    capsys.readouterr()


def seed_supervisor(front, sid=SUP):
    store.write_snapshot(paths.roster_path(), {"sessions": {sid: {
        "id": sid, "role": "supervisor", "pool": "opus", "model": "opus",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": iso(NOW - timedelta(minutes=30)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }}})


def tasks_by_title(front):
    return {task["title"]: task for task in store.fold_by_id(
        store.read_ledger(paths.front_tasks_path(front)))}


def folded_job(front, jid):
    return {job["id"]: job for job in store.fold_by_id(
        store.read_ledger(paths.front_jobs_path(front)))}[jid]


def seed_job(front, jid, task_id, units, state="returned", **fields):
    record = {
        "id": jid, "task": task_id, "kind": "implement", "role": "muse",
        "priority": 1, "spec_path": "/tmp/specs/job.md", "session": WRK,
        "worktree": "", "branch": "", "base": "", "log": "",
        "timeout": "20m", "units": units, "attempt": 1, "state": state,
        "planned_at": iso(NOW - timedelta(minutes=10)),
        "queued_at": iso(NOW - timedelta(minutes=9)),
        "started_at": iso(NOW - timedelta(minutes=8)),
        "returned_at": iso(NOW - timedelta(minutes=2))
        if state in ("returned", "returned-with-work") else None,
        "verified_at": None, "artifact": "", "verdict_path": "",
    }
    record.update(fields)
    store.append_ledger(paths.front_jobs_path(front), record)


def seed_worker_session(children, front, jid, sid=WRK, **fields):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    children.append(proc)
    record = entities.Session.from_dict({
        "id": sid, "role": "muse", "pool": "fake", "model": "fake-test-model",
        "front": front, "job": jid, "pid": proc.pid, "pgid": proc.pid,
        "pid_starttime": procs.proc_starttime(proc.pid),
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": SUP, "started_at": iso(NOW - timedelta(minutes=8)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }).to_dict()
    record.update(fields)
    roster = store.read_snapshot(paths.roster_path())
    roster["sessions"][sid] = record
    store.write_snapshot(paths.roster_path(), roster)
    return proc


def test_launch_units_takes_a_count_with_lacks_as_default(
        env, fake_pool, monkeypatch, capsys):
    """`--units 4` is four units; the default is what the task still lacks.

    `--units 4` used to name unit number four and credit one, so a task
    whose work was delivered could never reach `built`. Ranges and names
    are refused now, and zero stays a real count for repair jobs.
    """
    # One worker per front: each launch holds its job slot against the
    # front's ceiling, so spreading the launches keeps the ceiling out of
    # what this test is about.
    add_front(env, monkeypatch, capsys, name="flow")
    add_front(env, monkeypatch, capsys, name="flowb")
    add_front(env, monkeypatch, capsys, name="flowc")
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md")

    assert run(monkeypatch, ["launch", "muse", "fake", spec,
                             "--repo", str(repo),
                             "--front", "flow", "--task", "first",
                             "--units", "2"]) == 0
    capsys.readouterr()
    jobs = store.fold_by_id(
        store.read_ledger(paths.front_jobs_path("flow")))
    assert [job["units"] for job in jobs] == [2]

    assert run(monkeypatch, ["launch", "muse", "fake", spec,
                             "--repo", str(repo),
                             "--front", "flowb", "--task", "first"]) == 0
    capsys.readouterr()
    jobs = store.fold_by_id(
        store.read_ledger(paths.front_jobs_path("flowb")))
    assert [job["units"] for job in jobs] == [3]

    assert run(monkeypatch, ["launch", "muse", "fake", spec,
                             "--repo", str(repo),
                             "--front", "flowc", "--task", "first",
                             "--units", "0"]) == 0
    capsys.readouterr()
    jobs = store.fold_by_id(
        store.read_ledger(paths.front_jobs_path("flowc")))
    assert [job["units"] for job in jobs] == [0]

    assert run(monkeypatch, ["launch", "muse", "fake", spec,
                             "--repo", str(repo),
                             "--front", "flow", "--task", "first",
                             "--units", "2-4"]) != 0
    assert "bad units" in capsys.readouterr().err
    assert run(monkeypatch, ["launch", "muse", "fake", spec,
                             "--repo", str(repo),
                             "--front", "flow", "--task", "first",
                             "--units", "banana"]) != 0
    assert "bad units" in capsys.readouterr().err


def test_verify_credits_the_launch_count_or_the_named_override(
        env, monkeypatch, capsys):
    """Verifying credits the job's count; `--units n` credits n instead."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    seed_job("flow", "job-count1", first, 2)
    assert run(monkeypatch, ["job", "verify", "job-count1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 0
    out = capsys.readouterr().out
    assert "2 units" in out
    assert tasks_by_title("flow")["first"]["units_done"] == 2

    seed_job("flow", "job-count2", first, 2)
    assert run(monkeypatch, ["job", "verify", "job-count2",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok",
                             "--units", "5"], SUP) == 0
    assert tasks_by_title("flow")["first"]["units_done"] == 7

    seed_job("flow", "job-count3", first, 1)
    assert run(monkeypatch, ["job", "verify", "job-count3",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok",
                             "--units", "many"], SUP) == 1
    assert "bad units" in capsys.readouterr().err
    assert tasks_by_title("flow")["first"]["units_done"] == 7


def test_killed_job_with_moved_branch_verifies_with_because(
        env, monkeypatch, capsys):
    """A job that commits and is killed still counts, with `--because`.

    The branch holds one commit past its base; the verify needs the
    sentence, lands it on the job line and the screen, and credits the
    units. Without the sentence, or without the branch having moved, the
    refusal stands.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    repo = make_moved_repo(env / "repo")
    seed_job("flow", "job-killed1", first, 2, state="killed",
             worktree=str(repo), branch="job-branch", base="main")

    assert run(monkeypatch, ["job", "verify", "job-killed1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 1
    assert "'--because'" in capsys.readouterr().err

    because = "the branch holds the finished draft"
    assert run(monkeypatch, ["job", "verify", "job-killed1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok",
                             "--because", because], SUP) == 0
    out = capsys.readouterr().out
    assert because in out
    job = folded_job("flow", "job-killed1")
    assert job["state"] == "verified"
    assert job["verify_because"] == because
    assert tasks_by_title("flow")["first"]["units_done"] == 2
    assert run(monkeypatch, ["status"]) == 0
    assert because in capsys.readouterr().out

    seed_job("flow", "job-killed2", first, 2, state="killed",
             worktree=str(repo), branch="main", base="main")
    assert run(monkeypatch, ["job", "verify", "job-killed2",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok",
                             "--because", because], SUP) == 1
    _, err = capsys.readouterr()
    assert "has not moved" in err
    assert tasks_by_title("flow")["first"]["units_done"] == 2


def test_collector_marks_moved_branch_without_marker_returned_with_work(
        env, fake_pool, children, monkeypatch, capsys):
    """Branch moved and no finish marker is `returned-with-work`, not failed.

    A worker that commits and dies before writing the marker still did
    the work. The collector records that as its own state; a branch that
    never moved still reads killed.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    repo = make_moved_repo(env / "repo")
    seed_job("flow", "job-moved1", first, 2, state="running",
             worktree=str(repo), branch="job-branch", base="main")
    proc = seed_worker_session(children, "flow", "job-moved1")
    FakeAdapter.script[WRK] = {"transcript_mtime": NOW.timestamp(),
                               "cpu_s": 1.0,
                               "finish_present": False, "finish_rc": None}
    proc.kill()
    proc.wait()

    tick(now=NOW)

    job = folded_job("flow", "job-moved1")
    assert job["state"] == "returned-with-work"
    assert job["returned_at"] == iso(NOW)

    seed_job("flow", "job-still1", first, 1, state="running",
             worktree=str(repo), branch="main", base="main")
    proc = seed_worker_session(children, "flow", "job-still1")
    FakeAdapter.script[WRK] = {"transcript_mtime": NOW.timestamp(),
                               "cpu_s": 1.0,
                               "finish_present": False, "finish_rc": None}
    proc.kill()
    proc.wait()

    tick(now=NOW + timedelta(seconds=1))

    assert folded_job("flow", "job-still1")["state"] == "killed"


def test_clean_exit_returns_within_one_tick(env, fake_pool, children,
                                            monkeypatch, capsys):
    """A worker that exits clean reads returned after one tick, never
    running; a finish marker with a non-zero code reads failed."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    seed_job("flow", "job-clean1", first, 2, state="running")
    proc = seed_worker_session(children, "flow", "job-clean1")
    FakeAdapter.script[WRK] = {"transcript_mtime": NOW.timestamp(),
                               "cpu_s": 1.0,
                               "finish_present": True, "finish_rc": 0}
    proc.kill()
    proc.wait()

    tick(now=NOW)

    job = folded_job("flow", "job-clean1")
    assert job["state"] == "returned"
    assert job["returned_at"] == iso(NOW)
    assert job["exit_code"] == 0

    seed_job("flow", "job-crash1", first, 2, state="running")
    proc = seed_worker_session(children, "flow", "job-crash1")
    FakeAdapter.script[WRK] = {"transcript_mtime": NOW.timestamp(),
                               "cpu_s": 1.0,
                               "finish_present": True, "finish_rc": 3}
    proc.kill()
    proc.wait()

    tick(now=NOW + timedelta(seconds=1))

    crashed = folded_job("flow", "job-crash1")
    assert crashed["state"] == "failed"
    assert crashed["exit_code"] == 3


def test_verify_running_names_tick_and_write_times(
        env, monkeypatch, capsys):
    """Refusing a running job names the collector tick and the last write.

    The two times read a stale collector apart from an unfinished worker:
    here the tick is old and the worker wrote recently.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    workdir = env / "wt-run"
    workdir.mkdir(parents=True, exist_ok=True)
    latest = workdir / "draft.txt"
    latest.write_text("still writing\n", encoding="utf-8")
    os.utime(latest, (WRITE_TS, WRITE_TS))
    os.utime(workdir, (WRITE_TS, WRITE_TS))
    seed_job("flow", "job-run1", first, 2, state="running",
             worktree=str(workdir))
    store.write_snapshot(paths.observed_path(), {"at": TICK_AT})

    assert run(monkeypatch, ["job", "verify", "job-run1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 1
    _, err = capsys.readouterr()
    assert "not 'returned'" in err
    assert TICK_AT in err
    assert WRITE_AT in err


def test_built_refuses_short_and_accepts_overshoot(
        env, monkeypatch, capsys):
    """`task built` still refuses under count; an overshoot stays accepted."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    seed_job("flow", "job-short1", first, 1)
    assert run(monkeypatch, ["job", "verify", "job-short1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["task", "built", first], SUP) == 1
    assert "1/3" in capsys.readouterr().err

    seed_job("flow", "job-over1", first, 5)
    assert run(monkeypatch, ["job", "verify", "job-over1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["task", "built", first], SUP) == 0
    capsys.readouterr()
    assert tasks_by_title("flow")["first"]["state"] == "built"


def test_old_list_units_still_credit_their_length(
        env, monkeypatch, capsys):
    """A job written before the count change keeps counting what it did.

    Its record carries a unit list, and folding it credits the length —
    exactly what `job verify` always added — so the task's count moves
    by two, neither zero nor a guess.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    seed_job("flow", "job-old1", first, [1, 2])
    assert run(monkeypatch, ["job", "verify", "job-old1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 0
    capsys.readouterr()
    assert tasks_by_title("flow")["first"]["units_done"] == 2
