"""Scratch worktrees live under the state directory, never on /tmp.

``job verify --run`` and ``merge land`` used ``tempfile.TemporaryDirectory``
for the detached worktree they run a check in. On a machine whose ``/tmp``
is tmpfs that killed the shell. These tests drive the real verbs against
a fresh FOREMAN_STATE and watch ``$PWD`` from inside the check.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, paths, store
from foreman.caller import SESSION_ENV


NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
DESK = "ses-desk001"
WRK = "ses-wrk0001"

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
size = 1
after = []
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    return tmp_path


def _forbid_tempfile(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError(
            "scratch worktrees must not use tempfile.mkdtemp "
            "or tempfile.TemporaryDirectory")

    monkeypatch.setattr(tempfile, "mkdtemp", boom)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", boom)


def run(monkeypatch, argv, session=None, cwd=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    if cwd is not None:
        monkeypatch.chdir(cwd)
    return cli.main(argv)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def session_record(sid, role, front=None, **fields):
    base = {
        "id": sid, "role": role, "pool": "opus", "model": "opus",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": iso(NOW - timedelta(minutes=30)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }
    base.update(fields)
    return base


def seed_roster(*entries):
    store.write_snapshot(paths.roster_path(),
                         {"sessions": {entry["id"]: entry
                                       for entry in entries}})


def tasks_by_title(front):
    return {task["title"]: task for task in store.fold_by_id(
        store.read_ledger(paths.front_tasks_path(front)))}


def folded_job(front, jid):
    return {job["id"]: job for job in store.fold_by_id(
        store.read_ledger(paths.front_jobs_path(front)))}[jid]


def merges_by_id():
    return {row["id"]: row for row in store.fold_by_id(
        store.read_ledger(paths.merges_path()))}


def write_flow_brief(root: Path, name: str) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        FLOW_BRIEF.format(name=name), encoding="utf-8")
    if not (root / ".git").exists():
        for argv in (["init", "-q", "-b", "main"],
                     ["-c", "user.email=t@t", "-c", "user.name=t",
                      "commit", "-q", "--allow-empty", "-m", "root"]):
            subprocess.run(["git", "-C", str(root), *argv], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return brief_dir


def add_front(env, monkeypatch, capsys, name="flow"):
    assert run(monkeypatch, ["front", "add", str(write_flow_brief(env, name))]) == 0
    capsys.readouterr()


def seed_supervisor(front, sid=SUP):
    store.write_snapshot(paths.roster_path(),
                         {"sessions": {sid: entities.Session.from_dict(
                             session_record(sid, "supervisor", front)
                         ).to_dict()}})


def _returned_job(front, jid, task_id, units=1, session=WRK, **fields):
    record = {
        "id": jid, "task": task_id, "kind": "implement", "role": "muse",
        "priority": 1, "spec_path": "", "session": session,
        "worktree": "", "branch": "", "log": "", "timeout": "20m",
        "units": units, "attempt": 1, "state": "returned",
        "planned_at": iso(NOW - timedelta(minutes=10)),
        "queued_at": iso(NOW - timedelta(minutes=9)),
        "started_at": iso(NOW - timedelta(minutes=8)),
        "returned_at": iso(NOW - timedelta(minutes=2)),
        "verified_at": None, "artifact": "", "verdict_path": "",
    }
    record.update(fields)
    store.append_ledger(paths.front_jobs_path(front), record)


def _make_built_repo(path: Path) -> tuple[Path, str]:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "test")
    git(path, "commit", "-q", "--allow-empty", "-m", "root")
    git(path, "checkout", "-qb", "job-branch")
    (path / "built").write_text("the artifact\n", encoding="utf-8")
    git(path, "add", "built")
    git(path, "commit", "-qm", "the work")
    return path, git(path, "rev-parse", "HEAD")


def _set_verify(front: str, title: str, command: str) -> None:
    task = tasks_by_title(front)[title]
    store.append_ledger(paths.front_tasks_path(front),
                        dict(task, verify=command))


def make_repo(root: Path) -> tuple[Path, Path]:
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-qm", "seed")
    origin = root / "origin.git"
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    return repo, origin


def make_branch(repo: Path, name: str, filename: str) -> None:
    git(repo, "checkout", "-qb", name)
    (repo / filename).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", filename)
    git(repo, "commit", "-qm", name)
    git(repo, "checkout", "-q", "main")


def write_brief(repo: Path, name: str) -> Path:
    brief_dir = repo / f"brief-{name}"
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        FLOW_BRIEF.format(name=name).replace(
            "prefer    = 0\n",
            'prefer    = 0\nmerge     = "desk"\n'),
        encoding="utf-8")
    return brief_dir


def write_check(tmp_path: Path, command: str) -> None:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "foreman.toml").write_text(
        "[merge]\n"
        f"check = {json.dumps(command)}\n",
        encoding="utf-8")


def build_task(monkeypatch, capsys, front, title, supervisor=SUP):
    task = tasks_by_title(front)[title]
    jid = f"job-{front}-{title}"
    store.append_ledger(paths.front_jobs_path(front), {
        "id": jid, "task": task["id"], "kind": "implement",
        "role": "muse", "priority": 1, "spec_path": "",
        "session": None, "worktree": "", "branch": "", "log": "",
        "timeout": "20m", "units": [1], "attempt": 1,
        "state": "returned",
        "planned_at": iso(NOW - timedelta(minutes=10)),
        "queued_at": iso(NOW - timedelta(minutes=9)),
        "started_at": iso(NOW - timedelta(minutes=8)),
        "returned_at": iso(NOW - timedelta(minutes=2)),
        "verified_at": None, "artifact": "", "verdict_path": "",
    })
    assert run(monkeypatch, ["job", "verify", jid,
                             "--confirmed",
                             "--command", f"make check {title}",
                             "--output", "ok"], supervisor) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["task", "built", task["id"]],
               supervisor) == 0
    capsys.readouterr()
    return tasks_by_title(front)[title]["id"]


def test_scratch_worktree_dir_is_under_state(env):
    path = paths.scratch_worktree_dir("verify", "job-abc")
    assert path == paths.state_dir() / "worktrees" / "scratch" / "verify-job-abc"
    assert path.parent.is_dir()
    assert not path.exists()
    land = paths.scratch_worktree_dir("land", "mrg-xyz")
    assert land == paths.state_dir() / "worktrees" / "scratch" / "land-mrg-xyz"


def test_remove_scratch_tolerates_absence_and_deletes(env):
    path = paths.scratch_worktree_dir("verify", "job-gone")
    paths.remove_scratch(path)
    path.mkdir()
    (path / "marker").write_text("x", encoding="utf-8")
    paths.remove_scratch(path)
    assert not path.exists()
    paths.remove_scratch(path)


def test_job_verify_run_scratches_under_state_not_tmp(
        env, monkeypatch, capsys):
    """``job verify --run`` works in ``worktrees/scratch/verify-<job>``
    and removes it; Python tempfile is not consulted."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    pwd_file = env / "verify-pwd"
    _set_verify(
        "flow", "first",
        f"printf '%s\\n' \"$PWD\" | tee {shlex.quote(str(pwd_file))}")
    repo, head = _make_built_repo(env / "fixture")
    jid = "job-notmp"
    _returned_job("flow", jid, first, units=1,
                  worktree=str(repo), branch="job-branch", head=head)
    expected = paths.state_dir() / "worktrees" / "scratch" / f"verify-{jid}"
    _forbid_tempfile(monkeypatch)

    assert run(monkeypatch, ["job", "verify", jid, "--run"], SUP) == 0
    capsys.readouterr()
    cwd = Path(pwd_file.read_text(encoding="utf-8").strip())
    assert cwd.resolve() == expected.resolve()
    assert not expected.exists()
    job = folded_job("flow", jid)
    dest = Path(job["verify_output_ref"])
    assert dest.is_file()
    assert dest.parent == paths.session_dir(WRK) / "verify"
    assert dest.read_text(encoding="utf-8").strip() == str(cwd)


def test_merge_land_scratches_under_state_not_tmp(
        env, monkeypatch, capsys):
    """``merge land`` works in ``worktrees/scratch/land-<merge>``
    and removes it; Python tempfile is not consulted."""
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    pwd_file = env / "land-pwd"
    write_check(
        env, f"printf '%s\\n' \"$PWD\" | tee {shlex.quote(str(pwd_file))}")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    assert run(monkeypatch, ["merge", "request", "feat",
                             "--front", "flow", "--tasks", first,
                             "--target", "main"], SUP, cwd=repo) == 0
    mid = capsys.readouterr().out.split()[0]
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    expected = paths.state_dir() / "worktrees" / "scratch" / f"land-{mid}"
    _forbid_tempfile(monkeypatch)

    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 0
    capsys.readouterr()
    cwd = Path(pwd_file.read_text(encoding="utf-8").strip())
    assert cwd.resolve() == expected.resolve()
    assert not expected.exists()
    record = merges_by_id()[mid]
    assert record["result"] == "landed"
    log_path = Path(record["check_output_ref"])
    assert log_path == paths.merge_check_log_path(DESK, mid)
    assert log_path.is_file()
    assert log_path.read_text(encoding="utf-8").strip() == str(cwd)
