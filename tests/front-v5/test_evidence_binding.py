"""Evidence lines name the head and base they ran on.

A v5 write carries both shas and `evidence list` prints bound; an
old-front line with neither prints unbound. Tests that would stay
green without the binding are not in this file.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, fronts, paths, store
from foreman.caller import SESSION_ENV
from foreman.entities import Session

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
JOB_HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

V5_BRIEF = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "A target move invalidates recorded evidence.",
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

FLOW_BRIEF = '''\
name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 1

[[task]]
title = "first"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "true"
size = 1
after = []
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


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


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


def seed_supervisor(front, sid=SUP):
    store.write_snapshot(paths.roster_path(), {"sessions": {
        sid: Session.from_dict({
            "id": sid, "role": "supervisor", "pool": "opus", "model": "opus",
            "front": front, "job": None, "pid": None, "pgid": None,
            "worktree": "", "log": "", "timeout": "20m",
            "launched_by": "owner",
            "started_at": iso(NOW - timedelta(minutes=30)),
            "last_declared_at": None, "last_observed_at": None,
            "cpu_s": 0.0, "state": "running",
        }).to_dict(),
    }})


def add_v5(env: Path, monkeypatch, capsys, name: str = "harbor") -> str:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    _src, bare, sha = make_bare(env)
    brief_dir = env / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=str(bare)), encoding="utf-8")
    assert cli.main(["front", "add", str(brief_dir)]) == 0
    capsys.readouterr()
    record = fronts.read_front_record(name)
    work = (record or {}).get("repositories", [{}])[0].get("work") or "v5work"
    subprocess.run(["git", "-C", str(_src), "branch", work, "main"], check=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", "-q", "origin",
         f"+refs/heads/{work}:refs/heads/{work}"],
        check=True)
    return sha


def add_old(env: Path, monkeypatch, capsys, name: str = "flow") -> None:
    brief_dir = env / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        FLOW_BRIEF.format(name=name), encoding="utf-8")
    assert run(monkeypatch, ["front", "add", str(brief_dir)]) == 0
    capsys.readouterr()


def seed_returned_job(front: str, job_id: str, head: str = JOB_HEAD) -> None:
    store.append_ledger(paths.front_jobs_path(front), {
        "id": job_id, "task": "", "kind": "implement", "role": "grok",
        "session": "ses-wrk0001", "worktree": "", "branch": "job/x",
        "head": head, "log": "", "timeout": "20m", "units": 1,
        "attempt": 1, "state": "returned",
        "spec_path": "/specs/job.md",
    })


def test_v5_evidence_carries_head_and_base_and_lists_bound(
        env, monkeypatch, capsys):
    """A verify on a v5 front stores both shas; evidence list prints bound."""
    base_sha = add_v5(env, monkeypatch, capsys)
    seed_supervisor("harbor")
    seed_returned_job("harbor", "job-bind1")
    assert run(monkeypatch, ["job", "verify", "job-bind1",
                             "--confirmed", "--command", "true",
                             "--output", "all green"], SUP) == 0
    capsys.readouterr()
    evidence = store.read_ledger(paths.front_evidence_path("harbor"))
    assert len(evidence) == 1
    assert evidence[0]["head"] == JOB_HEAD
    assert evidence[0]["base"] == base_sha
    job = store.fold_by_id(
        store.read_ledger(paths.front_jobs_path("harbor")))[0]
    assert job["head"] == JOB_HEAD
    assert job["base"] == base_sha
    assert run(monkeypatch, ["evidence", "list", "harbor"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    label = f"bound {JOB_HEAD[:7]}/{base_sha[:7]}"
    assert label in out
    assert "unbound" not in out


def test_old_front_evidence_lists_unbound(env, monkeypatch, capsys):
    """A line with neither sha is accepted and evidence list prints unbound."""
    add_old(env, monkeypatch, capsys)
    seed_supervisor("flow")
    assert run(monkeypatch, ["evidence", "--on", "flow",
                             "--claim", "seen on an old front",
                             "--status", "PLAUSIBLE"], SUP) == 0
    capsys.readouterr()
    evidence = store.read_ledger(paths.front_evidence_path("flow"))
    assert len(evidence) == 1
    assert not evidence[0].get("head")
    assert not evidence[0].get("base")
    assert run(monkeypatch, ["evidence", "list", "flow"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert "unbound" in out
    assert "bound " not in out
