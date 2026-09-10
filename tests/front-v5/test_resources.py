"""A node names the resources it uses; running nodes hold them.

Every test drives the real CLI against a fresh FOREMAN_STATE with a
v5-shape front. Holding is derived from the tree fold: a running node
holds each of its resources, and a returned, failed or cancelled job
holds none.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import capacity, cli, config, fronts, paths, store
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.entities import Session
from foreman import launch as launch_module
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
)
TWO_BUILDER_TEAM = (
    "grok-4.6:high:1:supervisor",
    "grok-4.6:high:2:builder",
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


def iso(moment: datetime) -> str:
    return moment.isoformat()


def write_resources(text: str = "[resources]\nfixture-db = 1\n") -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(text, encoding="utf-8")


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
    for item in flags.get("resources") or []:
        argv.extend(["--resource", item])
    return argv


def add_ok(monkeypatch, capsys, **flags):
    assert run(monkeypatch, add_argv(**flags), SUP) == 0
    return capsys.readouterr().out.strip()


def folded_tree(front="v5shape"):
    return {line["id"]: line for line in store.fold_by_id(
        store.read_ledger(paths.front_tree_path(front)))}


def test_config_reads_the_resources_table(env):
    """[resources] name = count is the table; a bool is not a count."""
    write_resources("[resources]\nfixture-db = 1\nlock = true\n")
    settings = config.load()
    assert settings.resource_count("fixture-db") == 1
    assert settings.resource_names() == ["fixture-db"]
    assert settings.resource_count("lock") is None
    assert settings.resource_count("missing") is None


def test_node_add_stores_resources(env, monkeypatch, capsys):
    """--resource (repeatable) lands on the node; duplicates collapse."""
    write_resources("[resources]\nfixture-db = 1\nlock = 2\n")
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    nid = add_ok(monkeypatch, capsys, kind="milestone", node_id="mil-1",
                 resources=["fixture-db", "lock", "fixture-db"])
    assert nid == "mil-1"
    node = folded_tree()[nid]
    assert node["resources"] == ["fixture-db", "lock"]


def test_node_add_unknown_resource_is_refused(env, monkeypatch, capsys):
    """An unknown name is refused naming the [resources] table."""
    write_resources()
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(resources=["nope"]), SUP) == 1
    _, err = capsys.readouterr()
    assert "nope" in err
    assert "[resources]" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == []


def test_node_revise_stores_resources(env, monkeypatch, capsys):
    """node revise --resource replaces the claim."""
    write_resources("[resources]\nfixture-db = 1\nlock = 1\n")
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    nid = add_ok(monkeypatch, capsys, node_id="mil-1",
                 resources=["fixture-db"])
    capsys.readouterr()
    assert run(monkeypatch, [
        "node", "revise", "v5shape", nid, "--reason", "claim the lock",
        "--resource", "lock",
    ], SUP) == 0
    capsys.readouterr()
    assert folded_tree()[nid]["resources"] == ["lock"]


def test_resource_list_prints_held_and_holders(env, monkeypatch, capsys):
    """resource list prints name: held h / count c and the holding nodes.
    A running node holds; returned, failed and cancelled hold none."""
    write_resources()
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys, node_id="mil-1")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door", node_id="tsk-1")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                 title="use the db", role="builder", node_id="job-a",
                 resources=["fixture-db"])
    capsys.readouterr()
    assert run(monkeypatch, ["resource", "list"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["fixture-db: held 0 / count 1"]

    assert run(monkeypatch, [
        "node", "revise", "v5shape", job, "--reason", "started",
        "--state", "running",
    ], SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["resource", "list"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "fixture-db: held 1 / count 1",
        "  v5shape job-a",
    ]

    for state in ("returned", "failed", "cancelled"):
        assert run(monkeypatch, [
            "node", "revise", "v5shape", job, "--reason", state,
            "--state", state,
        ], SUP) == 0
        capsys.readouterr()
        assert run(monkeypatch, ["resource", "list"]) == 0
        out = capsys.readouterr().out
        assert out.splitlines() == ["fixture-db: held 0 / count 1"], state


def add_chain(monkeypatch, capsys, job_id="job-a", role="builder",
              resources=None, what="implement the door"):
    mil = add_ok(monkeypatch, capsys, title="milestone one",
                 node_id="mil-1")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door", node_id="tsk-1")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                 title="implement the door", role=role, node_id=job_id,
                 resources=resources or [], what=what)
    return mil, tsk, job


def test_tick_second_job_waits_resource_until_the_first_returns(
        env, monkeypatch, capsys, launch_spawn):
    """With fixture-db = 1, slots free, one tick starts one job and the
    other waits resource fixture-db; after the first returns, the next
    tick starts the second. resource list and job list print the holder
    and the reason."""
    write_resources(
        "[resources]\nfixture-db = 1\n\n"
        "[pool.grok]\ncap = 2\nroles = [\"grok\"]\n")
    add_front(env, monkeypatch, capsys, team=TWO_BUILDER_TEAM)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(
        monkeypatch, capsys, job_id="job-a", resources=["fixture-db"])
    second = add_ok(monkeypatch, capsys, parent="tsk-1", kind="job",
                    title="the next unit", role="builder", node_id="job-b",
                    resources=["fixture-db"])
    assert run(monkeypatch, ["job", "queue", "v5shape", first], SUP) == 0
    assert run(monkeypatch, ["job", "queue", "v5shape", second], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    tree = folded_tree()
    assert tree[first]["state"] == "running"
    assert tree[second]["state"] == "queued"
    assert tree[second]["waits"] == "resource fixture-db"
    job_id = tree[first]["job"]
    assert run(monkeypatch, ["resource", "list"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "fixture-db: held 1 / count 1",
        "  v5shape job-a",
    ]
    assert run(monkeypatch, ["job", "list", "v5shape"], SUP) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{first}  builder  grok  running {job_id}",
        f"{second}  builder  grok  waits: resource fixture-db",
    ]

    session_id = tree[first]["session"]
    capacity.release_for_session(session_id, iso(NOW), "returned")
    assert run(monkeypatch, [
        "node", "revise", "v5shape", first, "--reason", "returned",
        "--state", "returned",
    ], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW + timedelta(seconds=2))
    tree = folded_tree()
    assert tree[second]["state"] == "running"
    assert run(monkeypatch, ["resource", "list"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "fixture-db: held 1 / count 1",
        "  v5shape job-b",
    ]


def test_tick_a_node_with_no_resources_is_unchanged(
        env, monkeypatch, capsys, launch_spawn):
    """A job that names no resource still starts while fixture-db is held."""
    write_resources(
        "[resources]\nfixture-db = 1\n\n"
        "[pool.grok]\ncap = 2\nroles = [\"grok\"]\n")
    add_front(env, monkeypatch, capsys, team=TWO_BUILDER_TEAM)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(
        monkeypatch, capsys, job_id="job-a", resources=["fixture-db"])
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
    assert tree[second]["waits"] == "ready"
    tick(now=NOW + timedelta(seconds=2))
    tree = folded_tree()
    assert tree[second]["state"] == "running"


def test_status_prints_resource_lines_under_capacity(
        env, monkeypatch, capsys, launch_spawn):
    """status prints the resource lines under Capacity when any resource
    is configured."""
    write_resources()
    add_front(env, monkeypatch, capsys, team=TWO_BUILDER_TEAM)
    seed_supervisor("v5shape")
    _mil, _tsk, first = add_chain(
        monkeypatch, capsys, job_id="job-a", resources=["fixture-db"])
    assert run(monkeypatch, ["job", "queue", "v5shape", first], SUP) == 0
    capsys.readouterr()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    capacity_block = out.split("Capacity:\n", 1)[1].split("Overall:", 1)[0]
    assert "fixture-db: held 1 / count 1" in capacity_block
    assert capacity_block.rstrip().endswith("fixture-db: held 1 / count 1")


def test_status_omits_resource_lines_when_none_are_configured(
        env, monkeypatch, capsys):
    """No [resources] table: status does not print a resource line."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    capacity_block = out.split("Capacity:\n", 1)[1].split("Overall:", 1)[0]
    assert "fixture-db" not in capacity_block
    assert "[resources]" not in capacity_block
