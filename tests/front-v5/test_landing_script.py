"""job land queues a script item that lands a verified job onto work.

A world with a bare origin, a work branch and a job branch one commit
ahead. job land on an unverified node is refused; on a verified one it
queues a script item under the same parent.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, fronts, paths, store
from foreman.caller import SESSION_ENV
from foreman.entities import Session

SPEC_VERIFY = "true"

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"

V5_BRIEF = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Landing is a queued script.",
]
supervisor = "grok-4.6:high"
team = [
  "grok-4.6:high:1:supervisor",
  "grok-4.6:high:1:builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5work"
target = "main"
check = "true"
'''


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


def iso(moment: datetime) -> str:
    return moment.isoformat()


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def make_bare(root: Path) -> tuple[Path, Path, str]:
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
    monkeypatch.delenv(SESSION_ENV, raising=False)
    src, bare, sha = make_bare(env)
    assert cli.main(["front", "add",
                     str(write_v5(env, name, str(bare)))]) == 0
    capsys.readouterr()
    record = fronts.read_front_record(name)
    work = (record or {}).get("repositories", [{}])[0].get("work") or "v5work"
    subprocess.run(["git", "-C", str(src), "branch", work, "main"], check=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", "-q", "origin",
         f"+refs/heads/{work}:refs/heads/{work}"],
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


def add_chain(monkeypatch, capsys, job_id="job-a", role="builder"):
    mil = add_ok(monkeypatch, capsys, title="milestone one",
                 node_id="mil-1")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door", node_id="tsk-1")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                 title="implement the door", role=role, node_id=job_id)
    return mil, tsk, job


def folded_tree(front="v5shape"):
    return {line["id"]: line for line in store.fold_by_id(
        store.read_ledger(paths.front_tree_path(front)))}


def mark_verified(front: str, node_id: str, job_id: str = "job-ver001",
                  branch: str | None = None) -> str:
    """Attach a verified jobs.jsonl line to the tree node."""
    store.append_ledger(paths.front_jobs_path(front), {
        "id": job_id, "state": "verified", "task": "tsk-1",
        "branch": branch or f"job/{front}-{node_id}",
        "verified_at": iso(NOW),
    })
    node = folded_tree(front)[node_id]
    from foreman.progress import _write_node_revise
    _write_node_revise(front, node, SUP, "job verified", job=job_id)
    return job_id


def test_job_land_refuses_an_unverified_node(env, monkeypatch, capsys):
    """job land names job verify when the node's job has no verified line."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    before = store.read_ledger(paths.front_tree_path("v5shape"))
    assert run(monkeypatch, ["job", "land", "v5shape", node_id], SUP) == 1
    _out, err = capsys.readouterr()
    assert f"{node_id} has no recorded verify; job verify first" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == before


def test_job_land_queues_a_script_item_for_a_verified_node(
        env, monkeypatch, capsys):
    """A verified job gets a queued script sibling that lands it."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    mark_verified("v5shape", node_id)
    capsys.readouterr()
    assert run(monkeypatch, ["job", "land", "v5shape", node_id], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    item_id = out.strip()
    assert item_id
    item = folded_tree()[item_id]
    assert item["kind"] == "job"
    assert item["role"] == "script"
    assert item["lands"] == node_id
    assert item["after"] == [node_id]
    assert item["state"] == "queued"
    assert item["parent"] == tsk
    built = folded_tree()[node_id]
    assert built["state"] != "landed"


def test_job_land_refuses_when_a_landing_item_is_already_open(
        env, monkeypatch, capsys):
    """A second job land for the same node is refused while the first is open."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    mark_verified("v5shape", node_id)
    capsys.readouterr()
    assert run(monkeypatch, ["job", "land", "v5shape", node_id], SUP) == 0
    first = capsys.readouterr().out.strip()
    before = store.read_ledger(paths.front_tree_path("v5shape"))
    assert run(monkeypatch, ["job", "land", "v5shape", node_id], SUP) == 1
    _out, err = capsys.readouterr()
    assert "already open" in err
    assert node_id in err
    assert first in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == before
