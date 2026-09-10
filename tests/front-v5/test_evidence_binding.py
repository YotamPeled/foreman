"""Evidence lines name the head and base they ran on.

A v5 write carries both shas and `evidence list` prints bound; an
old-front line with neither prints unbound. Tests that would stay
green without the binding are not in this file.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, fronts, landing, paths, store
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.entities import Session
from foreman.progress import _write_node_revise

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
check = "{check}"
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


def add_v5(env: Path, monkeypatch, capsys,
           name: str = "harbor",
           check: str = "true") -> tuple[str, Path]:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    src, bare, sha = make_bare(env)
    brief_dir = env / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=str(bare), check=check),
        encoding="utf-8")
    assert cli.main(["front", "add", str(brief_dir)]) == 0
    capsys.readouterr()
    record = fronts.read_front_record(name)
    work = (record or {}).get("repositories", [{}])[0].get("work") or "v5work"
    subprocess.run(["git", "-C", str(src), "branch", work, "main"], check=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", "-q", "origin",
         f"+refs/heads/{work}:refs/heads/{work}"],
        check=True)
    return sha, bare


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


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def open_clone(env: Path, bare: Path) -> Path:
    clone = env / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    git(clone, "config", "user.email", "test@example.invalid")
    git(clone, "config", "user.name", "foreman-test")
    return clone


def add_origin_main_commit(clone: Path, filename: str = "main.txt",
                           body: str = "main\n") -> str:
    git(clone, "fetch", "-q", "origin")
    git(clone, "checkout", "-q", "-B", "main", "origin/main")
    (clone / filename).write_text(body, encoding="utf-8")
    git(clone, "add", filename)
    git(clone, "commit", "-qm", "target moved")
    git(clone, "push", "-q", "origin", "main")
    return git(clone, "rev-parse", "HEAD")


def add_argv(front="harbor", parent="harbor", kind="milestone",
             title="Front inputs", **flags):
    argv = ["node", "add", front, "--parent", parent, "--kind", kind,
            "--title", title,
            "--verify", flags.get("verify", "true"),
            "--must-not-touch", flags.get("must_not_touch",
                                          "the live state directory"),
            "--reason", flags.get("reason", "the tree needs this node"),
            "--break", flags.get("break_", "admit a job as a parent"),
            "--repo", flags.get("repo", "foreman")]
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    return argv


def add_ok(monkeypatch, capsys, **flags):
    assert run(monkeypatch, add_argv(**flags), SUP) == 0
    return capsys.readouterr().out.strip()


def folded_tree(front="harbor"):
    return {line["id"]: line for line in store.fold_by_id(
        store.read_ledger(paths.front_tree_path(front)))}


def rebase_items(front: str = "harbor") -> list[dict]:
    return [node for node in folded_tree(front).values()
            if node.get("kind") == "rebase"]


def test_v5_evidence_carries_head_and_base_and_lists_bound(
        env, monkeypatch, capsys):
    """A verify on a v5 front stores both shas; evidence list prints bound."""
    base_sha, _bare = add_v5(env, monkeypatch, capsys)
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


def test_rebase_marks_bound_evidence_stale_and_front_done_names_the_node(
        env, monkeypatch, capsys):
    """A target move stales the older line; front done waits on a new green."""
    base_sha, bare = add_v5(env, monkeypatch, capsys)
    seed_supervisor("harbor")
    mil = add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door", node_id="tsk-1")
    node_id = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                     title="implement the door", role="builder",
                     node_id="nod-x")
    seed_returned_job("harbor", "job-bind1")
    assert run(monkeypatch, ["job", "verify", "job-bind1",
                             "--confirmed", "--command", "true",
                             "--output", "all green"], SUP) == 0
    capsys.readouterr()
    node = folded_tree()[node_id]
    _write_node_revise("harbor", node, SUP, "landed",
                       state="landed", job="job-bind1")
    clone = open_clone(env, bare)
    moved = add_origin_main_commit(clone)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    items = rebase_items()
    assert len(items) == 1
    result = landing.run("harbor", items[0], by=SUP)
    assert result.ok, result.fail_reason
    assert run(monkeypatch, ["evidence", "list", "harbor"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert f"stale (base moved to {moved[:7]})" in out
    folded = store.fold_by_id(
        store.read_ledger(paths.front_evidence_path("harbor")))
    older = [line for line in folded if line.get("on") == "job-bind1"]
    assert older and older[0]["stale"] == moved
    rebase_lines = [line for line in folded if line.get("on") == "harbor"]
    assert rebase_lines
    assert rebase_lines[0]["base"] == moved
    assert not rebase_lines[0].get("stale")
    assert run(monkeypatch, ["front", "done", "harbor"], SUP) == 1
    _out, err = capsys.readouterr()
    assert f"front harbor has stale evidence on {node_id}; re-run its verify" \
        in err
    record = fronts.read_front_record("harbor")
    assert record is not None and record.get("state") != "done"
    assert run(monkeypatch, ["evidence", "--on", "job-bind1",
                             "--claim", "re-verified after the move",
                             "--status", "CONFIRMED", "--command", "true",
                             "--output", "green again"], SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["front", "done", "harbor"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == "harbor done"


def write_flake_check(env: Path, *, always_fail: bool,
                      test_name: str = "test_flaky") -> str:
    script = env / "flake_check.py"
    script.write_text(
        "import os, pathlib, sys\n"
        "p = pathlib.Path(os.environ['FLAKE_RUNS'])\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "n += 1\n"
        "p.write_text(str(n))\n"
        f"name = {test_name!r}\n"
        f"if n == 1 or {always_fail!r}:\n"
        "    print(f'FAILED tests/test_x.py::{name}')\n"
        "    sys.exit(1)\n"
        "print('ok')\n",
        encoding="utf-8")
    return f"{sys.executable} {script}"


def mark_verified(front: str, node_id: str, job_id: str = "job-ver001",
                  branch: str | None = None,
                  worktree: Path | str | None = None) -> str:
    store.append_ledger(paths.front_jobs_path(front), {
        "id": job_id, "state": "verified", "task": "tsk-1",
        "branch": branch or f"job/{front}-{node_id}",
        "worktree": str(worktree) if worktree else "",
        "verified_at": iso(NOW),
    })
    node = folded_tree(front)[node_id]
    _write_node_revise(front, node, SUP, "job verified", job=job_id)
    return job_id


def checkout_work(clone: Path, work: str = "v5work") -> str:
    git(clone, "fetch", "-q", "origin", work)
    git(clone, "checkout", "-q", "-B", work, f"origin/{work}")
    return git(clone, "rev-parse", "HEAD")


def add_job_commit(clone: Path, branch: str, filename: str = "feat.txt",
                   body: str = "feat\n") -> str:
    git(clone, "checkout", "-qb", branch)
    (clone / filename).write_text(body, encoding="utf-8")
    git(clone, "add", filename)
    git(clone, "commit", "-qm", "job work")
    sha = git(clone, "rev-parse", "HEAD")
    git(clone, "checkout", "-q", "-")
    return sha


def origin_work_sha(bare: Path, work: str = "v5work") -> str:
    return subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{work}"],
        check=True, capture_output=True, text=True).stdout.strip()


def queue_landing(monkeypatch, capsys, node_id: str, clone: Path,
                  front: str = "harbor") -> str:
    branch = f"job/{front}-{node_id}"
    mark_verified(front, node_id, worktree=clone, branch=branch)
    capsys.readouterr()
    assert run(monkeypatch, ["job", "land", front, node_id], SUP) == 0
    return capsys.readouterr().out.strip()


def _flake_world(env, monkeypatch, capsys, *, always_fail: bool,
                 test_name: str = "test_flaky"):
    monkeypatch.setenv("FLAKE_RUNS", str(env / "flake_runs.txt"))
    check = write_flake_check(env, always_fail=always_fail,
                              test_name=test_name)
    _sha, bare = add_v5(env, monkeypatch, capsys, check=check)
    seed_supervisor("harbor")
    mil = add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door", node_id="tsk-1")
    node_id = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                     title="implement the door", role="builder",
                     node_id="nod-x")
    clone = open_clone(env, bare)
    checkout_work(clone)
    add_job_commit(clone, f"job/harbor-{node_id}")
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    return bare, node_id, item_id


def test_registered_flake_retries_once_and_records_green(
        env, monkeypatch, capsys):
    """A landing whose only failure is a registered flake is green 1/2."""
    bare, node_id, item_id = _flake_world(
        env, monkeypatch, capsys, always_fail=False)
    assert run(monkeypatch, ["check", "flake", "harbor", node_id,
                             "--test", "test_flaky",
                             "--reason", "fails under contention"],
               SUP) == 0
    capsys.readouterr()
    before = origin_work_sha(bare)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    assert origin_work_sha(bare) != before
    item = folded_tree()[item_id]
    assert item["state"] == "landed"
    assert item["greens"] == 1
    assert item["runs"] == 2
    assert item["flake"] == "test_flaky"
    assert (env / "flake_runs.txt").read_text(encoding="utf-8") == "2"
    assert run(monkeypatch, ["job", "list", "harbor"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert f"{item_id}  script    green 1/2 (flake: test_flaky)" in out.splitlines()


def test_registered_flake_failing_twice_is_red(env, monkeypatch, capsys):
    """The same registered test failing on the retry stays red."""
    bare, node_id, item_id = _flake_world(
        env, monkeypatch, capsys, always_fail=True)
    assert run(monkeypatch, ["check", "flake", "harbor", node_id,
                             "--test", "test_flaky",
                             "--reason", "fails under contention"],
               SUP) == 0
    capsys.readouterr()
    before = origin_work_sha(bare)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    assert origin_work_sha(bare) == before
    item = folded_tree()[item_id]
    assert item["state"] == "failed"
    assert (env / "flake_runs.txt").read_text(encoding="utf-8") == "2"


def test_unregistered_failure_is_red_on_the_first_run(
        env, monkeypatch, capsys):
    """An unregistered failing test is never re-run."""
    bare, node_id, item_id = _flake_world(
        env, monkeypatch, capsys, always_fail=True, test_name="test_other")
    assert run(monkeypatch, ["check", "flake", "harbor", node_id,
                             "--test", "test_flaky",
                             "--reason", "a different test"],
               SUP) == 0
    capsys.readouterr()
    before = origin_work_sha(bare)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    assert origin_work_sha(bare) == before
    item = folded_tree()[item_id]
    assert item["state"] == "failed"
    assert (env / "flake_runs.txt").read_text(encoding="utf-8") == "1"
    assert item.get("greens") is None
    assert not item.get("flake")


