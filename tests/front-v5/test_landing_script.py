"""job land queues a script item that lands a verified job onto work.

A world with a bare origin, a work branch and a job branch one commit
ahead. job land on an unverified node is refused; on a verified one it
queues a script item under the same parent.
"""

from __future__ import annotations

import fcntl
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, fronts, landing, paths, store
from foreman.caller import SESSION_ENV
from foreman.collector import tick
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
                  branch: str | None = None,
                  worktree: Path | str | None = None) -> str:
    """Attach a verified jobs.jsonl line to the tree node."""
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


def origin_work_sha(bare: Path, work: str = "v5work") -> str:
    return subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{work}"],
        check=True, capture_output=True, text=True).stdout.strip()


def queue_landing(monkeypatch, capsys, node_id: str, clone: Path,
                  front: str = "v5shape") -> str:
    branch = f"job/{front}-{node_id}"
    mark_verified(front, node_id, worktree=clone, branch=branch)
    capsys.readouterr()
    assert run(monkeypatch, ["job", "land", front, node_id], SUP) == 0
    return capsys.readouterr().out.strip()


def scratch_left() -> list[str]:
    root = paths.state_dir() / "worktrees" / "scratch"
    if not root.exists():
        return []
    return [entry.name for entry in root.iterdir()]


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


def test_landing_run_pushes_the_rebased_head(env, monkeypatch, capsys):
    """A job branch one commit ahead lands: origin work moves, fields record."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    branch = f"job/v5shape-{node_id}"
    add_job_commit(clone, branch)
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    before_work = origin_work_sha(bare)
    item = folded_tree()[item_id]
    result = landing.run("v5shape", item, by=SUP)
    assert result.ok, result.fail_reason
    after_work = origin_work_sha(bare)
    assert after_work != before_work
    assert after_work == result.head
    recorded = folded_tree()[item_id]
    assert recorded["state"] == "landed"
    assert recorded["command"] == "true"
    assert recorded["exit"] == 0
    assert recorded["seconds"] is not None and recorded["seconds"] >= 0
    assert recorded["output_file"]
    assert Path(recorded["output_file"]).is_file()
    assert str(paths.state_dir()) in recorded["output_file"]
    assert recorded["head"] == result.head
    built = folded_tree()[node_id]
    assert built["state"] == "landed"
    assert built["landed_sha"] == result.head
    assert scratch_left() == []


def test_empty_range_fails_before_the_check(env, monkeypatch, capsys):
    """A branch with nothing new fails with the empty-range reason; no check."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    branch = f"job/v5shape-{node_id}"
    git(clone, "branch", branch, "v5work")
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    before_work = origin_work_sha(bare)
    item = folded_tree()[item_id]
    result = landing.run("v5shape", item, by=SUP)
    assert not result.ok
    assert "adds nothing to v5work@" in result.fail_reason
    assert branch in result.fail_reason
    assert result.command == ""
    assert result.exit is None
    recorded = folded_tree()[item_id]
    assert recorded["state"] == "failed"
    assert "adds nothing to v5work@" in recorded["fail_reason"]
    assert "command" not in recorded
    assert origin_work_sha(bare) == before_work
    assert folded_tree()[node_id]["state"] != "landed"
    assert scratch_left() == []


def test_rebase_conflict_names_the_file(env, monkeypatch, capsys):
    """A conflicting rebase fails naming the file; origin work does not move."""
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
    before_work = origin_work_sha(bare)
    result = landing.run("v5shape", folded_tree()[item_id], by=SUP)
    assert not result.ok
    assert result.fail_reason == "conflict.txt"
    recorded = folded_tree()[item_id]
    assert recorded["state"] == "failed"
    assert recorded["fail_reason"] == "conflict.txt"
    assert origin_work_sha(bare) == before_work
    assert folded_tree()[node_id]["state"] != "landed"
    assert scratch_left() == []


def test_red_check_does_not_push(env, monkeypatch, capsys):
    """A red check fails and origin work does not move."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys, check="false")
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    branch = f"job/v5shape-{node_id}"
    add_job_commit(clone, branch)
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    before_work = origin_work_sha(bare)
    result = landing.run("v5shape", folded_tree()[item_id], by=SUP)
    assert not result.ok
    assert result.exit != 0
    assert result.command == "false"
    recorded = folded_tree()[item_id]
    assert recorded["state"] == "failed"
    assert recorded["exit"] != 0
    assert recorded["command"] == "false"
    assert recorded["output_file"]
    assert Path(recorded["output_file"]).is_file()
    assert origin_work_sha(bare) == before_work
    assert folded_tree()[node_id]["state"] != "landed"
    assert scratch_left() == []


def test_dropped_lists_a_commit_already_on_work(env, monkeypatch, capsys):
    """A commit whose patch is already on work is listed under dropped."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    branch = f"job/v5shape-{node_id}"
    git(clone, "checkout", "-qb", branch)
    (clone / "shared.txt").write_text("shared\n", encoding="utf-8")
    git(clone, "add", "shared.txt")
    git(clone, "commit", "-qm", "shared change")
    dropped_sha = git(clone, "rev-parse", "HEAD")
    (clone / "only-job.txt").write_text("job only\n", encoding="utf-8")
    git(clone, "add", "only-job.txt")
    git(clone, "commit", "-qm", "job only")
    git(clone, "checkout", "-q", "v5work")
    git(clone, "cherry-pick", "-x", dropped_sha)
    git(clone, "push", "-q", "origin", "v5work")
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    result = landing.run("v5shape", folded_tree()[item_id], by=SUP)
    assert result.ok, result.fail_reason
    assert dropped_sha in result.dropped
    recorded = folded_tree()[item_id]
    assert dropped_sha in recorded["dropped"]
    assert scratch_left() == []


def test_second_front_waits_for_the_repository_lock(
        env, monkeypatch, capsys):
    """A second front's item on the same repository waits for the lock."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    branch = f"job/v5shape-{node_id}"
    add_job_commit(clone, branch)
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    lock_path = landing.lock_path_for(str(clone))
    held = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    outcome: list = []

    def hold():
        with open(lock_path, "a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            held.set()
            release.wait(timeout=10)
            fcntl.flock(handle, fcntl.LOCK_UN)

    def do_run():
        result = landing.run("v5shape", folded_tree()[item_id], by=SUP)
        outcome.append(result)
        finished.set()

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(timeout=2)
    runner = threading.Thread(target=do_run)
    runner.start()
    time.sleep(0.3)
    assert not finished.is_set()
    release.set()
    runner.join(timeout=30)
    holder.join(timeout=2)
    assert finished.is_set()
    assert outcome and outcome[0].ok, (
        outcome[0].fail_reason if outcome else "no result")
    assert scratch_left() == []


def test_one_tick_lands_the_script_item(env, monkeypatch, capsys):
    """A queued landing item is run by the tick: origin moves, job list
    prints landed <sha>, the built node is landed."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    branch = f"job/v5shape-{node_id}"
    add_job_commit(clone, branch)
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    before_work = origin_work_sha(bare)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    after_work = origin_work_sha(bare)
    assert after_work != before_work
    item = folded_tree()[item_id]
    assert item["state"] == "landed"
    assert item["command"] == "true"
    assert item["exit"] == 0
    assert item["seconds"] is not None and item["seconds"] >= 0
    assert item["output_file"]
    assert Path(item["output_file"]).is_file()
    assert str(paths.state_dir()) in item["output_file"]
    assert item["head"] == after_work
    built = folded_tree()[node_id]
    assert built["state"] == "landed"
    assert built["landed_sha"] == after_work
    assert scratch_left() == []
    assert run(monkeypatch, ["job", "list", "v5shape"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert f"{item_id}  script    landed {after_work}" in out.splitlines()


def test_job_list_prints_failed_reason(env, monkeypatch, capsys):
    """A red check is recorded failed and job list prints failed: <reason>."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys, check="false")
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, job_id="job-a")
    clone = open_clone(env, bare)
    checkout_work(clone)
    add_job_commit(clone, f"job/v5shape-{node_id}")
    item_id = queue_landing(monkeypatch, capsys, node_id, clone)
    before_work = origin_work_sha(bare)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    assert origin_work_sha(bare) == before_work
    item = folded_tree()[item_id]
    assert item["state"] == "failed"
    assert folded_tree()[node_id]["state"] != "landed"
    assert run(monkeypatch, ["job", "list", "v5shape"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert f"{item_id}  script    failed: {item['fail_reason']}" in out.splitlines()


def test_tick_one_script_per_repository(env, monkeypatch, capsys):
    """A second landing item on the same repository waits lock this tick."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, tsk, first = add_chain(monkeypatch, capsys, job_id="job-a")
    second = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                    title="the next unit", role="builder", node_id="job-b")
    clone = open_clone(env, bare)
    checkout_work(clone)
    add_job_commit(clone, f"job/v5shape-{first}", filename="a.txt")
    add_job_commit(clone, f"job/v5shape-{second}", filename="b.txt")
    first_item = queue_landing(monkeypatch, capsys, first, clone)
    mark_verified("v5shape", second, job_id="job-ver002", worktree=clone,
                  branch=f"job/v5shape-{second}")
    capsys.readouterr()
    assert run(monkeypatch, ["job", "land", "v5shape", second], SUP) == 0
    second_item = capsys.readouterr().out.strip()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    tree = folded_tree()
    assert tree[first_item]["state"] == "landed"
    assert tree[second_item]["state"] == "queued"
    assert tree[second_item]["waits"] == "lock"
    tick(now=NOW + timedelta(seconds=2))
    tree = folded_tree()
    assert tree[second_item]["state"] == "landed"
    assert scratch_left() == []
