"""job queue records a tree node as queued and why it waits.

Every test drives the real CLI against a fresh FOREMAN_STATE with a
v5-shape front whose repository is named ``foreman``.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import capacity, cli, entities, mcp as mcp_module, paths, store
from foreman.caller import SESSION_ENV

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
OTHER_SUP = "ses-sup0002"
WORKER = "ses-wrk0001"

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
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5"
target = "main"
check = "python -m pytest tests -q"
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


def make_bare(root: Path) -> tuple[Path, str]:
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
    return bare, sha


def write_v5(root: Path, name: str, url: str) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=url), encoding="utf-8")
    return brief_dir


def add_front(env: Path, monkeypatch, capsys, name: str = "v5shape") -> str:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    bare, sha = make_bare(env)
    assert cli.main(["front", "add", str(write_v5(env, name, str(bare)))]) == 0
    capsys.readouterr()
    return sha


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def session_entry(sid, role, front):
    return entities.Session.from_dict({
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
            "--verify", flags.get("verify", "python -m pytest tests -q"),
            "--must-not-touch", flags.get("must_not_touch",
                                          "the live state directory"),
            "--reason", flags.get("reason", "the tree needs this node"),
            "--break", flags.get("break_", "admit a job as a parent"),
            "--repo", flags.get("repo", "foreman")]
    if flags.get("role") is not None:
        argv.extend(["--role", flags["role"]])
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    for item in flags.get("after") or []:
        argv.extend(["--after", item])
    return argv


def add_ok(monkeypatch, capsys, **flags):
    assert run(monkeypatch, add_argv(**flags), SUP) == 0
    return capsys.readouterr().out.strip()


def add_chain(monkeypatch, capsys, job_id="job-a", role="builder",
              after=None):
    mil = add_ok(monkeypatch, capsys, title="milestone one",
                 node_id="mil-1")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door", node_id="tsk-1")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                 title="implement the door", role=role, node_id=job_id,
                 after=after or [])
    return mil, tsk, job


def folded_tree(front="v5shape"):
    return {line["id"]: line for line in store.fold_by_id(
        store.read_ledger(paths.front_tree_path(front)))}


def test_job_queue_refuses_a_task(env, monkeypatch, capsys):
    """A task is named by kind; only a job is queued, and nothing writes."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, tsk, _job = add_chain(monkeypatch, capsys)
    before = store.read_ledger(paths.front_tree_path("v5shape"))
    assert run(monkeypatch, ["job", "queue", "v5shape", tsk], SUP) == 1
    _, err = capsys.readouterr()
    assert f"{tsk} is kind task; only a job is queued" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == before


def test_job_queue_waits_dependency_when_after_is_unlanded(
        env, monkeypatch, capsys):
    """An unlanded after node is why the queued job waits."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(monkeypatch, capsys, job_id="job-a")
    second = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                    title="the next unit", role="builder", node_id="job-b",
                    after=[first])
    assert run(monkeypatch, ["job", "queue", "v5shape", second], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == f"queued {second} (waits: dependency {first})"
    node = folded_tree()[second]
    assert node["state"] == "queued"
    assert node["waits"] == f"dependency {first}"
    assert node["queued_at"]
    assert node["after"] == [first]
    assert node["op"] == "revise"


def test_job_queue_waits_ready_when_after_is_landed(
        env, monkeypatch, capsys):
    """A landed after node is not a wait; the job queues ready."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(monkeypatch, capsys, job_id="job-a")
    assert run(monkeypatch, ["node", "revise", "v5shape", first,
                             "--state", "landed",
                             "--reason", "predecessor landed"], SUP) == 0
    capsys.readouterr()
    second = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                    title="the next unit", role="builder", node_id="job-b",
                    after=[first])
    assert run(monkeypatch, ["job", "queue", "v5shape", second], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == f"queued {second} (waits: ready)"
    node = folded_tree()[second]
    assert node["state"] == "queued"
    assert node["waits"] == "ready"
    assert node["state"] != "running"


def test_job_queue_waits_no_slot_when_reserved_slot_is_held(
        env, monkeypatch, capsys):
    """A second job on a pool whose one reserved slot is held waits."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["front", "reserve", "v5shape",
                     "--phase", "builders"]) == 0
    capsys.readouterr()
    capacity.grant(pool="grok", front="v5shape", role="grok",
                   job="job-held", session="ses-held01")
    _mil, _tsk, job = add_chain(monkeypatch, capsys, job_id="job-a")
    assert run(monkeypatch, ["job", "queue", "v5shape", job], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == f"queued {job} (waits: no slot)"
    node = folded_tree()[job]
    assert node["waits"] == "no slot"
    assert node["state"] == "queued"


def test_job_queue_merges_after_and_stores_sheet_add(
        env, monkeypatch, capsys):
    """--after is the node's own list plus the flag; sheet_add is stored."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(monkeypatch, capsys, job_id="job-a")
    second = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                    title="the next unit", role="builder", node_id="job-b",
                    after=[first])
    extra = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                   title="another unit", role="builder", node_id="job-c")
    assert run(monkeypatch, ["node", "revise", "v5shape", extra,
                             "--state", "landed",
                             "--reason", "already done"], SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch,
               ["job", "queue", "v5shape", second, "--after", extra,
                "--sheet-add", "read the map first"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert f"waits: dependency {first}" in out
    node = folded_tree()[second]
    assert node["after"] == [first, extra]
    assert node["sheet_add"] == "read the map first"


def test_job_queue_refuses_already_queued_and_running(
        env, monkeypatch, capsys):
    """A queued or running job is named by state; the fold does not grow."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, job = add_chain(monkeypatch, capsys, job_id="job-a")
    assert run(monkeypatch, ["job", "queue", "v5shape", job], SUP) == 0
    capsys.readouterr()
    before = store.read_ledger(paths.front_tree_path("v5shape"))
    assert run(monkeypatch, ["job", "queue", "v5shape", job], SUP) == 1
    _, err = capsys.readouterr()
    assert f"{job} is queued; only a job with no state is queued" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == before
    other = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                   title="running one", role="builder", node_id="job-r")
    assert run(monkeypatch, ["node", "revise", "v5shape", other,
                             "--state", "running",
                             "--reason", "started by hand"], SUP) == 0
    capsys.readouterr()
    before = store.read_ledger(paths.front_tree_path("v5shape"))
    assert run(monkeypatch, ["job", "queue", "v5shape", other], SUP) == 1
    _, err = capsys.readouterr()
    assert f"{other} is running; only a job with no state is queued" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == before


def test_node_revise_unknown_state_is_refused(env, monkeypatch, capsys):
    """--state refuses any word that is not a NODE_STATES value."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, job = add_chain(monkeypatch, capsys, job_id="job-a")
    before = store.read_ledger(paths.front_tree_path("v5shape"))
    assert run(monkeypatch, ["node", "revise", "v5shape", job,
                             "--state", "planned",
                             "--reason", "not a node state"], SUP) == 1
    _, err = capsys.readouterr()
    assert "state" in err
    assert "planned" not in folded_tree()[job].get("state", "planned")
    assert store.read_ledger(paths.front_tree_path("v5shape")) == before


def test_job_queue_tool_is_listed_for_supervisor(env, monkeypatch):
    """job queue is a supervisor tool, hidden from a worker."""
    seed_roster(
        session_entry(SUP, "supervisor", "v5shape"),
        session_entry(WORKER, "muse", "v5shape"),
    )
    monkeypatch.setenv(SESSION_ENV, SUP)
    supervisor = {tool["name"] for tool in
                  mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list",
                                     "params": {}})["result"]["tools"]}
    assert "job_queue" in supervisor
    monkeypatch.setenv(SESSION_ENV, WORKER)
    worker = {tool["name"] for tool in
              mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list",
                                 "params": {}})["result"]["tools"]}
    assert "job_queue" not in worker
