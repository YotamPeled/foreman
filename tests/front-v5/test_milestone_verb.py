"""milestone add appends a ledger line; split/merge/list fold it back.

Every test drives the real CLI against a fresh FOREMAN_STATE with a
v5-shape front whose repository is named ``foreman``.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, mcp as mcp_module, paths, store
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


def add_argv(front="v5shape", title="Front inputs",
             done_when="the front takes the owner's inputs",
             verify="python -m pytest tests -q",
             reason="every later milestone writes onto the front record",
             break_="drop the goal key from a v5 brief", **flags):
    argv = ["milestone", "add", front, "--title", title,
            "--done-when", done_when, "--verify", verify,
            "--reason", reason, "--break", break_]
    if flags.get("order") is not None:
        argv.extend(["--order", str(flags["order"])])
    if flags.get("force"):
        argv.append("--force")
    return argv


def test_milestone_add_appends_every_field(env, monkeypatch, capsys):
    """The supervisor's add lands on milestones.jsonl with every field set."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(order=2), SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    mid = out.strip()
    assert mid.startswith("mil-")
    lines = store.read_ledger(paths.front_milestones_path("v5shape"))
    assert len(lines) == 1
    line = lines[0]
    for key in ("id", "front", "op", "order", "title", "done_when", "verify",
                "reason", "break", "from_ids", "at", "by"):
        assert key in line, key
    assert line["id"] == mid
    assert line["front"] == "v5shape"
    assert line["op"] == "add"
    assert line["order"] == 2
    assert line["title"] == "Front inputs"
    assert line["done_when"] == "the front takes the owner's inputs"
    assert line["verify"] == "python -m pytest tests -q"
    assert line["reason"] == "every later milestone writes onto the front record"
    assert line["break"] == "drop the goal key from a v5 brief"
    assert line["from_ids"] == []
    assert line["by"] == SUP
    assert line["at"]


def test_milestone_add_duplicate_title_is_refused(env, monkeypatch, capsys):
    """A second add with the same title is refused naming title."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(), SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch, add_argv(), SUP) == 1
    _, err = capsys.readouterr()
    assert "title" in err
    assert "Front inputs" in err
    assert len(store.read_ledger(paths.front_milestones_path("v5shape"))) == 1


def test_milestone_add_ninth_is_refused_unless_force(
        env, monkeypatch, capsys):
    """A ninth live milestone is refused unless --force --reason."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    for i in range(8):
        assert run(monkeypatch, add_argv(title=f"Milestone {i}"), SUP) == 0
        capsys.readouterr()
    assert run(monkeypatch, add_argv(title="Milestone 8"), SUP) == 1
    _, err = capsys.readouterr()
    assert "eight" in err
    assert len(store.read_ledger(paths.front_milestones_path("v5shape"))) == 8
    assert run(monkeypatch, add_argv(title="Milestone 8", force=True,
                                     reason="the front needs a ninth"),
               SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    ninth = out.strip()
    lines = store.read_ledger(paths.front_milestones_path("v5shape"))
    assert len(lines) == 9
    assert lines[-1]["id"] == ninth
    assert lines[-1]["force"] is True
    assert lines[-1]["reason"] == "the front needs a ninth"


def test_milestone_add_other_supervisor_is_refused(
        env, monkeypatch, capsys):
    """A supervisor of another front is refused by name."""
    add_front(env, monkeypatch, capsys)
    seed_roster(session_entry(OTHER_SUP, "supervisor", "elsewhere"))
    assert run(monkeypatch, add_argv(), OTHER_SUP) == 1
    _, err = capsys.readouterr()
    assert "elsewhere" in err
    assert "v5shape" in err
    assert store.read_ledger(paths.front_milestones_path("v5shape")) == []


def test_milestone_add_tool_is_listed_for_supervisor_not_worker(
        env, monkeypatch):
    """milestone_add is a supervisor tool and is hidden from a worker."""
    seed_roster(
        session_entry(SUP, "supervisor", "v5shape"),
        session_entry(WORKER, "muse", "v5shape"),
    )
    monkeypatch.setenv(SESSION_ENV, SUP)
    supervisor = {tool["name"] for tool in
                  mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list",
                                     "params": {}})["result"]["tools"]}
    assert "milestone_add" in supervisor
    monkeypatch.setenv(SESSION_ENV, WORKER)
    worker = {tool["name"] for tool in
              mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list",
                                 "params": {}})["result"]["tools"]}
    assert "milestone_add" not in worker


def test_milestone_add_unknown_front_and_empty_fields_are_refused(
        env, monkeypatch, capsys):
    """Unknown front and empty title, done-when, verify, reason, break."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    argv = ["milestone", "add", "missing", "--title", "",
            "--done-when", "", "--verify", "", "--reason", "", "--break", ""]
    assert run(monkeypatch, argv, SUP) == 1
    _, err = capsys.readouterr()
    assert "missing" in err
    assert "--title" in err
    assert "--done-when" in err
    assert "--verify" in err
    assert "--reason" in err
    assert "--break" in err
    assert store.read_ledger(paths.front_milestones_path("v5shape")) == []
