"""A failed landing files a finding on the front with the check output.

A red check copies the output under the front's findings directory and
names the finding on the item. A rebase conflict or empty range files
the reason alone. A green landing files nothing. Inbox stays empty.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, fronts, landing, paths, store
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
check = "{check}"
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


def write_v5(root: Path, name: str, url: str, check: str = "true") -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=url, check=check), encoding="utf-8")
    return brief_dir


def add_front(env: Path, monkeypatch, capsys, name: str = "v5shape",
              check: str = "true") -> tuple[Path, Path, str]:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    src, bare, sha = make_bare(env)
    assert cli.main(["front", "add",
                     str(write_v5(env, name, str(bare), check=check))]) == 0
    capsys.readouterr()
    record = fronts.read_front_record(name)
    work = (record or {}).get("repositories", [{}])[0].get("work") or "v5work"
    subprocess.run(["git", "-C", str(src), "branch", work, "main"], check=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", "-q", "origin",
         f"+refs/heads/{work}:refs/heads/{work}"],
        check=True)
    return src, bare, sha


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def session_entry(sid, role, front, state="running"):
    return Session.from_dict({
        "id": sid, "role": role, "pool": "opus", "model": "opus",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": iso(NOW - timedelta(minutes=30)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": state,
    }).to_dict()


def seed_roster(*entries):
    store.write_snapshot(
        paths.roster_path(),
        {"sessions": {entry["id"]: entry for entry in entries}})


def seed_supervisor(front, sid=SUP, state="running"):
    seed_roster(session_entry(sid, "supervisor", front, state=state))


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
                  branch: str | None = None,
                  worktree: Path | str | None = None) -> str:
    store.append_ledger(paths.front_jobs_path(front), {
        "id": job_id, "state": "verified", "task": "tsk-1",
        "branch": branch or f"job/{front}-{node_id}",
        "worktree": str(worktree) if worktree else "",
        "verified_at": iso(NOW),
    })
    node = folded_tree(front)[node_id]
    from foreman.progress import _write_node_revise
    _write_node_revise(front, node, SUP, "job verified", job=job_id)
    return job_id


def open_clone(env: Path, bare: Path) -> Path:
    clone = env / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    git(clone, "config", "user.email", "test@example.invalid")
    git(clone, "config", "user.name", "foreman-test")
    return clone


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


def queue_landing(monkeypatch, capsys, node_id: str, clone: Path,
                  front: str = "v5shape") -> str:
    branch = f"job/{front}-{node_id}"
    mark_verified(front, node_id, worktree=clone, branch=branch)
    capsys.readouterr()
    assert run(monkeypatch, ["job", "land", front, node_id], SUP) == 0
    return capsys.readouterr().out.strip()


def queue_front_land(monkeypatch, capsys, front: str = "v5shape") -> str:
    capsys.readouterr()
    assert run(monkeypatch, ["front", "land", front], None) == 0
    return capsys.readouterr().out.strip()


def add_work_commit(clone: Path, work: str = "v5work",
                    filename: str = "feat.txt", body: str = "feat\n") -> str:
    git(clone, "fetch", "-q", "origin", work)
    git(clone, "checkout", "-q", "-B", work, f"origin/{work}")
    (clone / filename).write_text(body, encoding="utf-8")
    git(clone, "add", filename)
    git(clone, "commit", "-qm", "work ahead")
    git(clone, "push", "-q", "origin", work)
    return git(clone, "rev-parse", "HEAD")


def findings(front: str = "v5shape") -> list[dict]:
    return store.read_ledger(paths.front_findings_path(front))


def test_red_check_files_one_landing_finding_with_the_output(
        env, monkeypatch, capsys):
    """A red check files one finding of class landing; evidence is the output."""
    _src, bare, _sha = add_front(
        env, monkeypatch, capsys, check="echo LANDING-RED; false")
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    branch = f"job/v5shape-{node_id}"
    add_job_commit(clone, branch)
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    result = landing.run("v5shape", folded_tree()[item_id], by=SUP)
    assert not result.ok
    rows = findings()
    assert len(rows) == 1
    finding = rows[0]
    assert finding["class"] == "landing"
    assert finding["on"] == "v5shape"
    first = result.fail_reason.splitlines()[0]
    assert finding["title"] == (
        f"landing of {branch} onto v5work failed: {first}")
    assert "command: echo LANDING-RED; false" in finding["detail"]
    assert f"exit: {result.exit}" in finding["detail"]
    assert "seconds:" in finding["detail"]
    assert "dropped:" in finding["detail"]
    assert result.fail_reason in finding["detail"]
    evidence = Path(finding["evidence_ref"])
    assert evidence.is_file()
    assert evidence.parent == paths.front_findings_dir("v5shape")
    assert "LANDING-RED" in evidence.read_text(encoding="utf-8")
    recorded = folded_tree()[item_id]
    assert recorded["finding"] == finding["id"]
    assert store.read_ledger(paths.inbox_path()) == []


def test_a_second_failed_run_files_a_second_finding(
        env, monkeypatch, capsys):
    """One finding per failed run: re-running a red item files another."""
    _src, bare, _sha = add_front(
        env, monkeypatch, capsys, check="echo LANDING-RED; false")
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    add_job_commit(clone, f"job/v5shape-{node_id}")
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    landing.run("v5shape", folded_tree()[item_id], by=SUP)
    landing.run("v5shape", folded_tree()[item_id], by=SUP)
    rows = findings()
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]
    assert all(row["class"] == "landing" for row in rows)
    recorded = folded_tree()[item_id]
    assert recorded["finding"] == rows[1]["id"]
    assert store.read_ledger(paths.inbox_path()) == []


def test_rebase_conflict_files_a_finding_naming_the_file(
        env, monkeypatch, capsys):
    """A conflicting rebase files a finding with the file named and no evidence."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    branch = f"job/v5shape-{node_id}"
    add_job_commit(clone, branch, filename="conflict.txt", body="job\n")
    git(clone, "checkout", "-q", "v5work")
    (clone / "conflict.txt").write_text("work\n", encoding="utf-8")
    git(clone, "add", "conflict.txt")
    git(clone, "commit", "-qm", "work side")
    git(clone, "push", "-q", "origin", "v5work")
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    result = landing.run("v5shape", folded_tree()[item_id], by=SUP)
    assert not result.ok
    assert result.fail_reason == "conflict.txt"
    rows = findings()
    assert len(rows) == 1
    finding = rows[0]
    assert finding["class"] == "landing"
    assert "conflict.txt" in finding["title"]
    assert finding["title"] == (
        f"landing of {branch} onto v5work failed: conflict.txt")
    assert finding["detail"].endswith("conflict.txt")
    assert finding.get("evidence_ref", "") == ""
    assert not paths.front_findings_dir("v5shape").exists()
    recorded = folded_tree()[item_id]
    assert recorded["finding"] == finding["id"]
    assert store.read_ledger(paths.inbox_path()) == []


def test_empty_range_files_a_finding_with_the_reason_alone(
        env, monkeypatch, capsys):
    """An empty-range failure attaches the reason and copies no output file."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    branch = f"job/v5shape-{node_id}"
    git(clone, "branch", branch, "v5work")
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    result = landing.run("v5shape", folded_tree()[item_id], by=SUP)
    assert not result.ok
    assert "adds nothing to v5work@" in result.fail_reason
    rows = findings()
    assert len(rows) == 1
    finding = rows[0]
    assert finding["class"] == "landing"
    assert result.fail_reason.splitlines()[0] in finding["title"]
    assert finding.get("evidence_ref", "") == ""
    assert not paths.front_findings_dir("v5shape").exists()
    assert folded_tree()[item_id]["finding"] == finding["id"]
    assert store.read_ledger(paths.inbox_path()) == []


def test_green_landing_files_no_finding(env, monkeypatch, capsys):
    """A landing that succeeds writes no finding and leaves the inbox empty."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    add_job_commit(clone, f"job/v5shape-{node_id}")
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    result = landing.run("v5shape", folded_tree()[item_id], by=SUP)
    assert result.ok, result.fail_reason
    assert findings() == []
    recorded = folded_tree()[item_id]
    assert "finding" not in recorded
    assert store.read_ledger(paths.inbox_path()) == []


def test_front_landing_red_check_files_a_finding(
        env, monkeypatch, capsys):
    """A failed front-landing item files one landing finding with the output."""
    _src, bare, _sha = add_front(
        env, monkeypatch, capsys, check="echo FRONT-RED; false")
    seed_supervisor("v5shape")
    add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    clone = open_clone(env, bare)
    add_work_commit(clone)
    item_id = queue_front_land(monkeypatch, capsys)
    result = landing.run("v5shape", folded_tree()[item_id], by=SUP)
    assert not result.ok
    rows = findings()
    assert len(rows) == 1
    finding = rows[0]
    assert finding["class"] == "landing"
    assert finding["title"].startswith("landing of v5work onto main failed:")
    evidence = Path(finding["evidence_ref"])
    assert evidence.is_file()
    assert "FRONT-RED" in evidence.read_text(encoding="utf-8")
    assert folded_tree()[item_id]["finding"] == finding["id"]
    assert store.read_ledger(paths.inbox_path()) == []
