"""launch refuses an unknown --task; job repoint moves a finished job.

Every test drives the real entry points against a fresh FOREMAN_STATE
and FOREMAN_CONFIG. A launch that names no task on the front must not
mint a session, cut a worktree or append a job line; a finished job
can be re-pointed by that front's supervisor only.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, paths, store
from foreman.caller import SESSION_ENV
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter


NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
OTHER_SUP = "ses-sup0002"

SPEC_OK = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v0 -q\n"
)

BRIEF = '''name      = "{name}"
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
title = "the real one"
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
title = "the other one"
scope = """
WHAT: do the second thing.
INPUTS: the first artifact.
OUTPUTS: the second artifact.
OUT OF SCOPE: everything else.
"""
verify = "make check second"
size = 2
after = []
'''


class FakeAdapter(PoolAdapter):
    """Fake pool: writes the pid file and returns, spawning nothing real."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    def launch(self, ctx: LaunchContext) -> int:
        pid = os.getpid()
        ctx.pid_path.write_text(f"{pid}\n", encoding="utf-8")
        return pid

    def observe(self, session: Session) -> dict:
        return {"transcript_mtime": None, "cpu_s": 0.0, "finish_present": False}

    def command_str(self, ctx: LaunchContext) -> str:
        return f"fake-exec --worktree {ctx.worktree}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    _git(tmp_path, "init", "-b", "main", "-q", str(tmp_path))
    _git(tmp_path, "-C", str(tmp_path), "commit", "-q", "--allow-empty",
         "-m", "init")
    (tmp_path / "spec.md").write_text(SPEC_OK, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def fake_pool():
    from foreman import pools

    pools.register("fake", FakeAdapter())
    try:
        yield FakeAdapter()
    finally:
        pools.unregister("fake")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.invalid",
         "-c", "user.name=foreman-test", *args],
        cwd=str(root), check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def add_front(root: Path, name: str = "f") -> None:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(BRIEF.format(name=name),
                                          encoding="utf-8")
    assert cli.main(["front", "add", str(brief_dir)]) == 0


def tasks_by_title(front: str) -> dict[str, dict]:
    return {task["title"]: task for task in store.fold_by_id(
        store.read_ledger(paths.front_tasks_path(front)))}


def folded_job(front: str, jid: str) -> dict:
    return {job["id"]: job for job in store.fold_by_id(
        store.read_ledger(paths.front_jobs_path(front)))}[jid]


def roster_sessions() -> dict:
    return store.read_snapshot(paths.roster_path(),
                               default={"sessions": {}}).get("sessions") or {}


def worktrees() -> list[Path]:
    root = paths.state_dir() / "worktrees"
    if not root.is_dir():
        return []
    return [path for path in root.iterdir() if path.is_dir()]


def seed_supervisor(front: str, sid: str = SUP) -> None:
    store.write_snapshot(paths.roster_path(), {"sessions": {
        sid: entities.Session(
            id=sid, role="supervisor", pool="opus", model="opus",
            front=front, state="running",
            started_at=iso(NOW - timedelta(minutes=30)),
        ).to_dict()}})


def seed_job(front: str, jid: str, task_id: str, units: int, state: str) -> None:
    store.append_ledger(paths.front_jobs_path(front), {
        "id": jid, "task": task_id, "kind": "implement", "role": "muse",
        "priority": 1, "spec_path": "/tmp/specs/job.md", "session": "ses-wrk0001",
        "worktree": "", "branch": "foreman/job", "log": "", "timeout": "20m",
        "units": units, "attempt": 1, "state": state,
        "planned_at": iso(NOW - timedelta(minutes=10)),
        "queued_at": iso(NOW - timedelta(minutes=9)),
        "started_at": iso(NOW - timedelta(minutes=8)),
        "returned_at": iso(NOW - timedelta(minutes=2))
        if state != "running" else None,
        "verified_at": iso(NOW - timedelta(minutes=1))
        if state == "verified" else None,
        "artifact": "", "verdict_path": "",
    })


def launch_argv(root: Path, *extra: str) -> list[str]:
    return ["launch", "muse", "fake", str(root / "spec.md"),
            "--repo", str(root), *extra]


def test_launch_refuses_unknown_task_and_writes_nothing(
        env, fake_pool, capsys):
    """`--task "no such"` names the given title, the front and the nearest.

    A title no task carries used to mint a session, cut a worktree and
    append a job line; only `job verify` refused afterwards. The refusal
    is collected before anything is created, so a dry run is refused the
    same way.
    """
    add_front(env, "f")
    argv = launch_argv(env, "--front", "f", "--task", "no such")
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "--task 'no such' names no task on front 'f'" in err
    assert "nearest: 'the real one'" in err
    assert roster_sessions() == {}
    assert worktrees() == []
    assert store.read_ledger(paths.front_jobs_path("f")) == []

    assert cli.main(argv + ["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "--task 'no such' names no task on front 'f'" in err
    assert "nearest: 'the real one'" in err
    assert roster_sessions() == {}
    assert worktrees() == []
    assert store.read_ledger(paths.front_jobs_path("f")) == []


def test_launch_refuses_task_without_front(env, fake_pool, capsys):
    """A task belongs to a front: `--task` without `--front` is refused."""
    argv = launch_argv(env, "--task", "the real one")
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "--task is refused without --front" in err
    assert "a task belongs to a front" in err
    assert roster_sessions() == {}
    assert worktrees() == []
