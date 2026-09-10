"""The collector starts the oldest startable queued job per front.

Every test drives the real CLI and ``start_queued`` against a fresh
FOREMAN_STATE with a v5-shape front whose repository is named
``foreman``. Worker spawn is substituted at ``spawn_and_wait`` — the
same last door before a unit — so nothing here reaches systemd.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import capacity, cli, fronts, paths, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.entities import Session
from foreman.launch import start_queued
from foreman.pools import _common as pool_common
from foreman.progress import _write_node_revise

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
    "grok-4.6:high:1:backup-builder",
)
ONE_BUILDER_TEAM = (
    "grok-4.6:high:1:supervisor",
    "grok-4.6:high:1:builder",
)


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
        # A pid that is not this process: a later tick must not observe
        # the test runner as the worker and kill it on timeout.
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


def test_tick_starts_the_oldest_ready_job(
        env, monkeypatch, capsys, launch_spawn):
    """One tick starts the oldest ready job; the captured command names
    its pool, branch and --headless. The newer ready job stays queued."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(monkeypatch, capsys, job_id="job-a")
    second = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                    title="the next unit", role="builder", node_id="job-b")
    assert run(monkeypatch, ["job", "queue", "v5shape", first], SUP) == 0
    assert run(monkeypatch, ["job", "queue", "v5shape", second], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    args = launch_args(launch_spawn)
    assert len(args) == 1, launch_spawn
    launched = args[0]
    assert launched.pool == "grok"
    assert launched.branch == f"job/v5shape-{first}"
    assert launched.headless is True
    tree = folded_tree()
    assert tree[first]["state"] == "running"
    assert tree[second]["state"] == "queued"
    starts = store.read_snapshot(paths.collector_path())["queue_starts"]
    assert starts[0]["front"] == "v5shape"
    assert starts[0]["node"] == first
    assert starts[0]["job"] == tree[first]["job"]


def test_tick_does_not_start_a_job_waiting_on_an_unlanded_after(
        env, monkeypatch, capsys, launch_spawn):
    """A job whose after is unlanded is not started and waits dependency."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(monkeypatch, capsys, job_id="job-a")
    second = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                    title="the next unit", role="builder", node_id="job-b",
                    after=[first])
    assert run(monkeypatch, ["job", "queue", "v5shape", second], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    assert launch_args(launch_spawn) == []
    node = folded_tree()[second]
    assert node["state"] == "queued"
    assert node["waits"] == f"dependency {first}"


def test_tick_second_job_waits_no_slot_until_the_slot_frees(
        env, monkeypatch, capsys, launch_spawn):
    """With the one reserved slot held, the second job waits no slot;
    a later tick after the slot frees starts it."""
    add_front(env, monkeypatch, capsys, team=ONE_BUILDER_TEAM)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(monkeypatch, capsys, job_id="job-a")
    second = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                    title="the next unit", role="builder", node_id="job-b")
    assert run(monkeypatch, ["job", "queue", "v5shape", first], SUP) == 0
    assert run(monkeypatch, ["job", "queue", "v5shape", second], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    tree = folded_tree()
    assert tree[first]["state"] == "running"
    assert tree[second]["state"] == "queued"
    assert tree[second]["waits"] == "no slot"
    session_id = tree[first]["session"]
    capacity.release_for_session(session_id, iso(NOW), "returned")
    tick(now=NOW + timedelta(seconds=2))
    tree = folded_tree()
    assert tree[second]["state"] == "running"
    args = launch_args(launch_spawn)
    assert [item.branch for item in args] == [
        f"job/v5shape-{first}", f"job/v5shape-{second}"]


def test_tick_backup_builder_waits_until_a_job_of_its_node_has_failed(
        env, monkeypatch, capsys, launch_spawn):
    """A backup-builder node waits until a job of its node has failed."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(
        monkeypatch, capsys, job_id="job-a", role="backup-builder")
    assert run(monkeypatch, ["job", "queue", "v5shape", node_id], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    assert launch_args(launch_spawn) == []
    node = folded_tree()[node_id]
    assert node["state"] == "queued"
    assert node["waits"] == "backup builder: no failed run"
    failed_id = "job-fail01"
    store.append_ledger(paths.front_jobs_path("v5shape"), {
        "id": failed_id, "state": "failed", "role": "grok",
    })
    _write_node_revise(
        "v5shape", node, "collector", "prior run failed", job=failed_id)
    tick(now=NOW + timedelta(seconds=2))
    tree = folded_tree()
    assert tree[node_id]["state"] == "running"
    assert len(launch_args(launch_spawn)) == 1


def test_tick_script_node_waits_script_runner_and_holds_no_slot(
        env, monkeypatch, capsys, launch_spawn):
    """A script node waits script runner and takes no slot."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, node_id = add_chain(
        monkeypatch, capsys, job_id="job-s", role="script")
    assert run(monkeypatch, ["job", "queue", "v5shape", node_id], SUP) == 0
    capsys.readouterr()
    before = list(capacity.open_grants())
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    assert launch_args(launch_spawn) == []
    node = folded_tree()[node_id]
    assert node["state"] == "queued"
    assert node["waits"] == "script runner"
    assert capacity.open_grants() == before


def test_two_ticks_with_nothing_changed_append_no_revise_line(
        env, monkeypatch, capsys, launch_spawn):
    """A wait reason already on the node is not rewritten every tick."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(monkeypatch, capsys, job_id="job-a")
    second = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                    title="the next unit", role="builder", node_id="job-b",
                    after=[first])
    assert run(monkeypatch, ["job", "queue", "v5shape", second], SUP) == 0
    capsys.readouterr()
    before = store.read_ledger(paths.front_tree_path("v5shape"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    tick(now=NOW + timedelta(seconds=2))
    after = store.read_ledger(paths.front_tree_path("v5shape"))
    assert after == before
    assert launch_args(launch_spawn) == []


def test_job_list_shows_tick_reasons_and_running_job(
        env, monkeypatch, capsys, launch_spawn):
    """job list prints running <job id> for started nodes and the tick's
    wait reason for the rest."""
    add_front(env, monkeypatch, capsys, team=ONE_BUILDER_TEAM)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(monkeypatch, capsys, job_id="job-a")
    second = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                    title="the next unit", role="builder", node_id="job-b")
    assert run(monkeypatch, ["job", "queue", "v5shape", first], SUP) == 0
    assert run(monkeypatch, ["job", "queue", "v5shape", second], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    tree = folded_tree()
    job_id = tree[first]["job"]
    assert run(monkeypatch, ["job", "list", "v5shape"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.splitlines() == [
        f"{first}  builder  grok  running {job_id}",
        f"{second}  builder  grok  waits: no slot",
    ]


def test_status_shows_queue_counts_for_a_v5_front(
        env, monkeypatch, capsys, launch_spawn):
    """status shows queue: N waiting, M running on a v5 front."""
    add_front(env, monkeypatch, capsys, team=ONE_BUILDER_TEAM)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(monkeypatch, capsys, job_id="job-a")
    second = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                    title="the next unit", role="builder", node_id="job-b")
    assert run(monkeypatch, ["job", "queue", "v5shape", first], SUP) == 0
    assert run(monkeypatch, ["job", "queue", "v5shape", second], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    assert cli.main(["status"]) == 0
    out, err = capsys.readouterr()
    assert "queue: 1 waiting, 1 running" in out
