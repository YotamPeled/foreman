"""Review death, retries, rounds and redesign.

Tests that would stay green without the work are not in this file.
Worker spawn is substituted at ``spawn_and_wait``; the collector's pool
adapter is a scripted fake. Nothing here reaches systemd or the live
state directory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, fronts, paths, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter
from foreman.pools import _common as pool_common
from foreman.progress import _write_node_revise

SPEC_VERIFY = "python -m pytest tests -q"

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
WRK = "ses-rev0001"
WRK2 = "ses-rev0002"
WRK3 = "ses-rev0003"
JOB = "job-rev0001"
JOB2 = "job-rev0002"
JOB3 = "job-rev0003"

VERDICT_CORRECTNESS = (
    '{"passed": false, "summary": "wrong", "class": "correctness",'
    ' "findings": [{"title": "bug", "detail": "x", "class": "correctness"}]}'
)
VERDICT_CLEAN = (
    '{"passed": true, "summary": "ok", "class": "clean", "findings": []}'
)

V5_BRIEF = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
]
supervisor = "grok-4.6:high"
team = [
{team}
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5work"
target = "main"
check = "python -m pytest tests -q"
'''

DEFAULT_TEAM = (
    "grok-4.6:high:1:supervisor",
    "grok-4.6:high:1:builder",
    "grok-4.6:high:1:reviewer",
)


class FakeAdapter(PoolAdapter):
    """Scripted pool: observe and refusal are per-session tables."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    script: dict[str, dict] = {}
    refusals: dict[str, dict | None] = {}

    def observe(self, session: Session) -> dict:
        return dict(self.script.get(session.id or "", {
            "transcript_mtime": None,
            "cpu_s": 0.0,
            "finish_present": False,
            "finish_rc": None,
        }))

    def refusal(self, session: Session) -> dict | None:
        if session.id in self.refusals:
            return self.refusals[session.id]
        return None

    def launch(self, ctx: LaunchContext) -> int:
        return 0

    def command_str(self, ctx: LaunchContext) -> str:
        return "fake-exec"


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
    """Capture ``cmd_launch`` args and refuse a real systemd unit."""
    calls: list = []

    def fake_spawn(argv, *, pid_path, session_id, popen):
        pid = 2_000_000 + len(calls)
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


@pytest.fixture()
def fake_pool():
    from foreman import pools

    FakeAdapter.script = {}
    FakeAdapter.refusals = {}
    pools.register("fake", FakeAdapter())
    try:
        yield FakeAdapter
    finally:
        pools.unregister("fake")


@pytest.fixture()
def children():
    import subprocess
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


def sleeper(children):
    import subprocess
    import sys
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    children.append(proc)
    return proc


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


def write_v5(root: Path, name: str, url: str,
             team: tuple[str, ...] = DEFAULT_TEAM) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    team_toml = ",\n".join(f'  "{entry}"' for entry in team)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=url, team=team_toml), encoding="utf-8")
    return brief_dir


def add_front(env: Path, monkeypatch, capsys, name: str = "v5shape",
              team: tuple[str, ...] = DEFAULT_TEAM) -> str:
    import subprocess
    monkeypatch.delenv(SESSION_ENV, raising=False)
    src, bare, sha = make_bare(env)
    assert cli.main(["front", "add",
                     str(write_v5(env, name, str(bare), team=team))]) == 0
    capsys.readouterr()
    record = fronts.read_front_record(name)
    work = (record or {}).get("repositories", [{}])[0].get("work") or "v5work"
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


def session_entry(sid, role, front, **fields):
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
    return Session.from_dict(base).to_dict()


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
    if flags.get("mechanical"):
        argv.append("--mechanical")
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    for item in flags.get("after") or []:
        argv.extend(["--after", item])
    return argv


def add_ok(monkeypatch, capsys, **flags):
    assert run(monkeypatch, add_argv(**flags), SUP) == 0
    return capsys.readouterr().out.strip()


def add_chain(monkeypatch, capsys, job_id="job-a", role="reviewer"):
    mil = add_ok(monkeypatch, capsys, title="milestone one",
                 node_id="mil-1")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door", node_id="tsk-1")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                 title="review the door", role=role, node_id=job_id)
    return mil, tsk, job


def folded_tree(front="v5shape"):
    return {line["id"]: line for line in store.fold_by_id(
        store.read_ledger(paths.front_tree_path(front)))}


def folded_job(front, job_id):
    for line in store.fold_by_id(store.read_ledger(paths.front_jobs_path(front))):
        if line.get("id") == job_id:
            return line
    return None


def job_lines(front, job_id):
    return [line for line in store.read_ledger(paths.front_jobs_path(front))
            if line.get("id") == job_id]


def seed_review_job(front, job_id, node_id, session=WRK, **fields):
    line = {
        "id": job_id, "task": "tsk-1", "kind": "review", "role": "astra",
        "state": "running", "session": session,
        "started_at": iso(NOW - timedelta(minutes=5)),
    }
    line.update(fields)
    store.append_ledger(paths.front_jobs_path(front), line)
    node = folded_tree(front)[node_id]
    _write_node_revise(front, node, SUP, "started",
                       state="running", job=job_id, session=session, waits="")
    return line


def seed_dead_review(env, children, front, job_id, *,
                     finish=True, finish_rc=0, refusal=None,
                     verdict=None, session=WRK):
    """A dead worker session whose tick will mark the review job."""
    proc = sleeper(children)
    FakeAdapter.script[session] = {
        "transcript_mtime": NOW.timestamp(),
        "cpu_s": 1.0,
        "finish_present": finish,
        "finish_rc": finish_rc,
    }
    FakeAdapter.refusals[session] = refusal
    if verdict is not None:
        path = paths.session_verdict_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(verdict, encoding="utf-8")
    seed_roster(
        session_entry(SUP, "supervisor", front),
        session_entry(session, "astra", front, pool="fake",
                      model="fake-test-model", job=job_id,
                      pid=proc.pid, pgid=proc.pid,
                      pid_starttime=_starttime(proc.pid)),
    )
    proc.kill()
    proc.wait()
    return proc


def return_review(env, children, front, job_id, node_id, verdict,
                  session, moment):
    """Tick a running review to returned with ``verdict``."""
    seed_review_job(front, job_id, node_id, session=session)
    seed_dead_review(env, children, front, job_id, refusal=None,
                     verdict=verdict, session=session)
    tick(now=moment)


def _starttime(pid):
    from foreman import procs
    return procs.proc_starttime(pid)


def test_quota_refusal_marks_review_died_not_a_verdict(
        env, fake_pool, children, monkeypatch, capsys):
    """A review whose adapter reports a quota refusal is died (quota).

    A clean finish marker would otherwise mark it returned; the refusal
    is the recorded state, and no verdict path is stored.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    seed_review_job("v5shape", JOB, node_id)
    seed_dead_review(
        env, children, "v5shape", JOB,
        refusal={"kind": "quota", "reset": "2026-09-14T00:00:00Z",
                 "detail": "quota exhausted"})
    tick(now=NOW)
    job = folded_job("v5shape", JOB)
    assert job["state"] == "died"
    assert job["died_because"] == "quota"
    assert not job.get("verdict_path")
    assert job.get("returned_at") in (None, "")
    assert run(monkeypatch, ["job", "list", "v5shape"], SUP) == 0
    out, _err = capsys.readouterr()
    assert "died (quota)" in out


def test_access_refusal_marks_review_died_access(
        env, fake_pool, children, monkeypatch, capsys):
    """An access flag on the adapter is died (access), never returned."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    seed_review_job("v5shape", JOB, node_id)
    seed_dead_review(
        env, children, "v5shape", JOB,
        refusal={"kind": "access", "detail": "access flagged"})
    tick(now=NOW)
    job = folded_job("v5shape", JOB)
    assert job["state"] == "died"
    assert job["died_because"] == "access"
    assert run(monkeypatch, ["job", "list", "v5shape"], SUP) == 0
    out, _err = capsys.readouterr()
    assert "died (access)" in out


def test_review_with_no_verdict_file_is_died_no_verdict(
        env, fake_pool, children, monkeypatch, capsys):
    """A review that exited with a clean finish but no verdict file is
    died (no verdict), not returned."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    seed_review_job("v5shape", JOB, node_id)
    seed_dead_review(env, children, "v5shape", JOB, refusal=None)
    tick(now=NOW)
    job = folded_job("v5shape", JOB)
    assert job["state"] == "died"
    assert job["died_because"] == "no verdict"
    assert not job.get("verdict_path")
    assert run(monkeypatch, ["job", "list", "v5shape"], SUP) == 0
    out, _err = capsys.readouterr()
    assert "died (no verdict)" in out


def test_implement_job_without_verdict_still_returns(
        env, fake_pool, children, monkeypatch, capsys):
    """``died`` is a review state: an implement job with a clean finish
    and no verdict file is still returned."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys, role="builder")
    capsys.readouterr()
    seed_review_job("v5shape", JOB, node_id, kind="implement", role="grok")
    seed_dead_review(env, children, "v5shape", JOB, refusal=None)
    tick(now=NOW)
    job = folded_job("v5shape", JOB)
    assert job["state"] == "returned"


def test_retry_keeps_both_lines_with_retry_of(
        env, fake_pool, children, monkeypatch, capsys):
    """``job retry`` queues a new review for the same node; the died
    line is never rewritten and both stay on the ledger with retry_of."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    seed_review_job("v5shape", JOB, node_id)
    seed_dead_review(
        env, children, "v5shape", JOB,
        refusal={"kind": "quota", "reset": "2026-09-14T00:00:00Z",
                 "detail": "quota exhausted"})
    tick(now=NOW)
    before = job_lines("v5shape", JOB)
    assert before[-1]["state"] == "died"
    assert run(monkeypatch, ["job", "retry", "v5shape", JOB], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert f"retry of {JOB}" in out
    retry_id = out.split()[0]
    after_died = job_lines("v5shape", JOB)
    assert [line["state"] for line in after_died] == [
        line["state"] for line in before]
    assert after_died[-1]["state"] == "died"
    retry = folded_job("v5shape", retry_id)
    assert retry["retry_of"] == JOB
    assert retry["kind"] == "review"
    assert retry["state"] == "queued"
    node = folded_tree()[node_id]
    assert node["state"] == "queued"
    assert node["job"] == retry_id
    assert node["retry_of"] == JOB
    assert run(monkeypatch, ["job", "list", "v5shape"], SUP) == 0
    listed, _err = capsys.readouterr()
    assert "died (quota)" in listed
    assert f"retry of {JOB}" in listed


def test_retry_refuses_a_returned_job(
        env, fake_pool, monkeypatch, capsys):
    """Nothing else creates a retry: a returned review is named by state."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    seed_review_job("v5shape", JOB, node_id, state="returned")
    before = store.read_ledger(paths.front_jobs_path("v5shape"))
    assert run(monkeypatch, ["job", "retry", "v5shape", JOB], SUP) == 1
    _out, err = capsys.readouterr()
    assert "only a died review is retried" in err
    assert store.read_ledger(paths.front_jobs_path("v5shape")) == before


def launch_args(calls):
    return [item["args"] for item in calls if item.get("kind") == "launch"]


def test_two_correctness_rounds_set_redesign_and_tick_starts_nothing(
        env, fake_pool, children, monkeypatch, capsys, launch_spawn):
    """Two verdicts of class correctness set the node to redesign; the
    tick starts nothing under it. job list prints redesign (twice:)."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys)
    sibling = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                     title="the next unit", role="builder", node_id="job-b")
    capsys.readouterr()
    return_review(env, children, "v5shape", JOB, node_id,
                  VERDICT_CORRECTNESS, WRK, NOW)
    job = folded_job("v5shape", JOB)
    assert job["state"] == "returned"
    assert job["review_class"] == "correctness"
    return_review(env, children, "v5shape", JOB2, node_id,
                  VERDICT_CORRECTNESS, WRK2, NOW + timedelta(seconds=2))
    node = folded_tree()[node_id]
    assert node["state"] == "redesign"
    assert node["review_rounds"] == [
        {"job": JOB, "class": "correctness"},
        {"job": JOB2, "class": "correctness"},
    ]
    assert run(monkeypatch, ["job", "list", "v5shape"], SUP) == 0
    listed, _err = capsys.readouterr()
    assert "redesign (twice: correctness)" in listed
    assert run(monkeypatch, ["job", "queue", "v5shape", sibling], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW + timedelta(seconds=4))
    tree = folded_tree()
    assert tree[node_id]["state"] == "redesign"
    assert tree[sibling]["state"] == "running"
    started = [item.branch for item in launch_args(launch_spawn)]
    assert started == [f"job/v5shape-{sibling}"]


def test_clean_round_between_correctness_does_not_redesign(
        env, fake_pool, children, monkeypatch, capsys):
    """A clean round between two correctness rounds is not twice the
    same class: the node is not redesign."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    return_review(env, children, "v5shape", JOB, node_id,
                  VERDICT_CORRECTNESS, WRK, NOW)
    return_review(env, children, "v5shape", JOB2, node_id,
                  VERDICT_CLEAN, WRK2, NOW + timedelta(seconds=2))
    return_review(env, children, "v5shape", JOB3, node_id,
                  VERDICT_CORRECTNESS, WRK3, NOW + timedelta(seconds=4))
    node = folded_tree()[node_id]
    assert node["state"] != "redesign"
    assert [item["class"] for item in node.get("review_rounds") or []] == [
        "correctness", "clean", "correctness"]


def test_node_revise_queued_clears_redesign(
        env, fake_pool, children, monkeypatch, capsys):
    """The supervisor clears redesign with node revise --state queued."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    return_review(env, children, "v5shape", JOB, node_id,
                  VERDICT_CORRECTNESS, WRK, NOW)
    return_review(env, children, "v5shape", JOB2, node_id,
                  VERDICT_CORRECTNESS, WRK2, NOW + timedelta(seconds=2))
    assert folded_tree()[node_id]["state"] == "redesign"
    assert run(monkeypatch, ["node", "revise", "v5shape", node_id,
                             "--state", "queued",
                             "--reason", "redesigned the approach"],
               SUP) == 0
    capsys.readouterr()
    node = folded_tree()[node_id]
    assert node["state"] == "queued"
    assert node.get("review_rounds") in (None, [])
