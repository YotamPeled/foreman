"""The collector starts the oldest startable queued job per front.

Every test drives the real CLI and ``start_queued`` against a fresh
FOREMAN_STATE with a v5-shape front whose repository is named
``foreman``. Worker spawn is substituted at ``spawn_and_wait`` — the
same last door before a unit — so nothing here reaches systemd.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, fronts, paths, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.entities import Session
from foreman.launch import start_queued
from foreman.pools import _common as pool_common

SPEC_VERIFY = "python -m pytest tests -q"

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"

V5_BRIEF = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
]
supervisor = "grok-4.6:high"
team = [
  "grok-4.6:high:1:supervisor",
  "grok-4.6:high:1:builder",
  "grok-4.6:high:1:backup-builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5work"
target = "main"
check = "python -m pytest tests -q"
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    import subprocess
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


@pytest.fixture()
def launch_spawn(env, monkeypatch):
    """Capture ``cmd_launch`` args and refuse a real systemd unit.

    Production calls ``cmd_launch`` then the pool adapter's
    ``spawn_and_wait``. The adapter double writes the pid file the
    launcher waits on; the captured Namespace is the command the tick
    would have printed (pool, branch, ``--headless``).
    """
    calls: list = []

    def fake_spawn(argv, *, pid_path, session_id, popen):
        pid = os.getpid()
        Path(pid_path).write_text(f"{pid}\n", encoding="utf-8")
        calls.append({"kind": "spawn", "argv": list(argv),
                      "session": session_id})
        return pid

    real = launch_module.cmd_launch

    def wrapped(args):
        calls.append({"kind": "launch", "args": args})
        return real(args)

    monkeypatch.setattr(pool_common, "spawn_and_wait", fake_spawn)
    monkeypatch.setattr(launch_module, "cmd_launch", wrapped)
    return calls


def iso(moment: datetime) -> str:
    return moment.isoformat()


def make_bare(root: Path) -> tuple[Path, Path, str]:
    import subprocess
    src = root / "src-repo"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    bare = root / "remote.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src), str(bare)],
                   check=True)
    return src, bare, sha


def write_v5(root: Path, name: str, url: str) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=url), encoding="utf-8")
    return brief_dir


def add_front(env: Path, monkeypatch, capsys, name: str = "v5shape") -> str:
    import subprocess
    monkeypatch.delenv(SESSION_ENV, raising=False)
    src, bare, sha = make_bare(env)
    assert cli.main(["front", "add", str(write_v5(env, name, str(bare)))]) == 0
    capsys.readouterr()
    record = fronts.read_front_record(name)
    work = (record or {}).get("repositories", [{}])[0].get("work") or "v5work"
    # The work branch is absent at front add (the brief refuses it if
    # the remote already has it). A running front has since created it;
    # the tick cuts job branches from that branch.
    subprocess.run(
        ["git", "-C", str(src), "branch", work, "main"], check=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", "-q", "origin",
         f"+refs/heads/{work}:refs/remotes/origin/{work}"],
        check=True)
    return sha


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def session_entry(sid, role, front):
    return Session.from_dict({
        "id": sid, "role": role, "pool": "opus", "model": "opus",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": iso(NOW - timedelta(minutes=30)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }).to_dict()


def seed_roster(*entries):
    store.write_snapshot(
        paths.roster_path(),
        {"sessions": {entry["id"]: entry for entry in entries}})


def seed_supervisor(front, sid=SUP):
    seed_roster(session_entry(sid, "supervisor", front))


def add_argv(front="v5shape", parent="v5shape", kind="milestone",
             title="Front inputs", **flags):
    argv = ["node", "add", front, "--parent", parent, "--kind", kind,
            "--title", title,
            "--verify", flags.get("verify", SPEC_VERIFY),
            "--must-not-touch", flags.get("must_not_touch",
                                          "the live state directory"),
            "--reason", flags.get("reason", "the tree needs this node"),
            "--break", flags.get("break_", "admit a job as a parent"),
            "--repo", flags.get("repo", "foreman")]
    if flags.get("role") is not None:
        argv.extend(["--role", flags["role"]])
    if flags.get("what") is not None:
        argv.extend(["--what", flags["what"]])
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    for item in flags.get("after") or []:
        argv.extend(["--after", item])
    return argv


def add_ok(monkeypatch, capsys, **flags):
    assert run(monkeypatch, add_argv(**flags), SUP) == 0
    return capsys.readouterr().out.strip()


def add_chain(monkeypatch, capsys, job_id="job-a", role="builder",
              after=None, what="implement the door"):
    mil = add_ok(monkeypatch, capsys, title="milestone one",
                 node_id="mil-1")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door", node_id="tsk-1")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                 title="implement the door", role=role, node_id=job_id,
                 after=after or [], what=what)
    return mil, tsk, job


def folded_tree(front="v5shape"):
    return {line["id"]: line for line in store.fold_by_id(
        store.read_ledger(paths.front_tree_path(front)))}


def launch_args(calls):
    return [item["args"] for item in calls if item.get("kind") == "launch"]


def test_start_queued_launches_through_cmd_launch(
        env, monkeypatch, capsys, launch_spawn):
    """A queued node becomes a running job: pool, branch and --headless
    are the ones the hand launch would have been given, and the node
    records session and job."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(
        monkeypatch, capsys, job_id="job-a",
        what="Cut the oldest ready job from the queue.")
    assert run(monkeypatch, ["job", "queue", "v5shape", node_id], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    job_id, reason = start_queued("v5shape", node_id, by="collector")
    assert reason == "", reason
    assert job_id and job_id.startswith("job-")
    args = launch_args(launch_spawn)
    assert len(args) == 1, launch_spawn
    launched = args[0]
    assert launched.pool == "grok"
    assert launched.role == "grok"
    assert launched.branch == f"job/v5shape-{node_id}"
    assert launched.headless is True
    assert launched.front == "v5shape"
    assert launched.base == "v5work"
    node = folded_tree()[node_id]
    assert node["state"] == "running"
    assert node["job"] == job_id
    assert node["session"]
    assert node["op"] == "revise"
    jobs = store.fold_by_id(store.read_ledger(paths.front_jobs_path("v5shape")))
    by_id = {row["id"]: row for row in jobs}
    assert by_id[job_id]["session"] == node["session"]
    assert by_id[job_id]["state"] == "running"
    spec = Path(launched.spec).read_text(encoding="utf-8")
    assert "Cut the oldest ready job from the queue." in spec
    assert "the live state directory" in spec
    assert SPEC_VERIFY in spec


def test_start_queued_refuses_a_node_that_is_not_queued(
        env, monkeypatch, capsys, launch_spawn):
    """An unstarted node is named; cmd_launch is not called."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    capsys.readouterr()
    job_id, reason = start_queued("v5shape", node_id, by="collector")
    assert job_id is None
    assert "not queued" in reason or "unstarted" in reason
    assert launch_args(launch_spawn) == []


def test_start_queued_returns_none_when_the_launcher_refuses(
        env, monkeypatch, capsys, launch_spawn):
    """A frozen swarm is a launcher refusal, and the node stays queued."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    assert run(monkeypatch, ["job", "queue", "v5shape", node_id], SUP) == 0
    capsys.readouterr()
    paths.frozen_path().write_text("frozen\n", encoding="utf-8")
    job_id, reason = start_queued("v5shape", node_id, by="collector")
    assert job_id is None
    assert reason
    assert folded_tree()[node_id]["state"] == "queued"
