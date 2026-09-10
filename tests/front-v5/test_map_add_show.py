"""map add appends a fact; map show folds it back.

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


def add_argv(front="v5shape", repo="foreman", section="Storage",
             fact="store.py is the one writer", **flags):
    argv = ["map", "add", front, "--repo", repo, "--section", section,
            "--fact", fact]
    if flags.get("seen", True) and not flags.get("assumed"):
        argv.append("--seen")
    if flags.get("assumed"):
        argv.append("--assumed")
        if flags.get("seen") is True and flags.get("assumed"):
            argv.append("--seen")
    if "where" in flags:
        if flags["where"] is not None:
            argv.extend(["--where", flags["where"]])
    elif flags.get("seen", True) and not flags.get("assumed"):
        argv.extend(["--where", "src/foreman/store.py"])
    if flags.get("commit"):
        argv.extend(["--commit", flags["commit"]])
    for src in flags.get("derived_from") or []:
        argv.extend(["--from", src])
    return argv


def test_map_add_appends_every_field(env, monkeypatch, capsys):
    """The supervisor's fact lands on map.jsonl with every field set."""
    sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(), SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    fid = out.strip()
    assert fid.startswith("map-")
    lines = store.read_ledger(paths.front_map_path("v5shape"))
    assert len(lines) == 1
    line = lines[0]
    for key in ("id", "front", "repo", "section", "text", "basis", "refs",
                "seen_at", "seen_where", "commit", "derived_from", "at", "by"):
        assert key in line, key
    assert line["id"] == fid
    assert line["front"] == "v5shape"
    assert line["repo"] == "foreman"
    assert line["section"] == "Storage"
    assert line["text"] == "store.py is the one writer"
    assert line["basis"] == "seen"
    assert line["refs"] == []
    assert line["seen_where"] == "src/foreman/store.py"
    assert line["commit"] == sha
    assert line["derived_from"] == []
    assert line["by"] == SUP
    assert line["seen_at"]
    assert line["at"]


def test_map_add_unknown_repo_is_refused(env, monkeypatch, capsys):
    """--repo other names the known repositories and writes nothing."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(repo="other"), SUP) == 1
    _, err = capsys.readouterr()
    assert "other" in err
    assert "foreman" in err
    assert store.read_ledger(paths.front_map_path("v5shape")) == []


def test_map_add_both_bases_is_refused(env, monkeypatch, capsys):
    """Passing both --seen and --assumed is refused, all at once."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(seen=True, assumed=True,
                                     where="src/foreman/store.py"),
               SUP) == 1
    _, err = capsys.readouterr()
    assert "seen" in err
    assert "assumed" in err
    assert store.read_ledger(paths.front_map_path("v5shape")) == []


def test_map_add_other_supervisor_is_refused(env, monkeypatch, capsys):
    """A supervisor of another front is refused by name."""
    add_front(env, monkeypatch, capsys)
    seed_roster(session_entry(OTHER_SUP, "supervisor", "elsewhere"))
    assert run(monkeypatch, add_argv(), OTHER_SUP) == 1
    _, err = capsys.readouterr()
    assert "elsewhere" in err
    assert "v5shape" in err
    assert store.read_ledger(paths.front_map_path("v5shape")) == []


def test_map_add_tool_is_listed_for_supervisor_not_worker(
        env, monkeypatch):
    """map_add is a supervisor tool and is hidden from a worker session."""
    seed_roster(
        session_entry(SUP, "supervisor", "v5shape"),
        session_entry(WORKER, "muse", "v5shape"),
    )
    monkeypatch.setenv(SESSION_ENV, SUP)
    supervisor = {tool["name"] for tool in
                  mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list",
                                     "params": {}})["result"]["tools"]}
    assert "map_add" in supervisor
    monkeypatch.setenv(SESSION_ENV, WORKER)
    worker = {tool["name"] for tool in
              mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list",
                                 "params": {}})["result"]["tools"]}
    assert "map_add" not in worker


def test_map_add_same_text_is_two_facts(env, monkeypatch, capsys):
    """Two adds with the same text are two facts; nothing is deduped."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(), SUP) == 0
    first = capsys.readouterr().out.strip()
    assert run(monkeypatch, add_argv(), SUP) == 0
    second = capsys.readouterr().out.strip()
    assert first != second
    lines = store.read_ledger(paths.front_map_path("v5shape"))
    assert [line["id"] for line in lines] == [first, second]
    assert lines[0]["text"] == lines[1]["text"]


def test_map_add_seen_without_where_is_refused(env, monkeypatch, capsys):
    """--seen without --where is refused and writes nothing."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    argv = ["map", "add", "v5shape", "--repo", "foreman",
            "--section", "Storage", "--fact", "a fact", "--seen"]
    assert run(monkeypatch, argv, SUP) == 1
    _, err = capsys.readouterr()
    assert "--where" in err
    assert store.read_ledger(paths.front_map_path("v5shape")) == []


def test_map_add_from_assumed_is_recorded_assumed(
        env, monkeypatch, capsys):
    """A fact --from an assumed fact is assumed even when given --seen."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(fact="the queue is the source",
                                     assumed=True, seen=False), SUP) == 0
    parent = capsys.readouterr().out.strip()
    assert run(monkeypatch,
               add_argv(fact="derived from the queue",
                        derived_from=[parent]), SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    child = out.splitlines()[0].strip()
    assert parent in out
    assert "assumed" in out
    lines = store.fold_by_id(store.read_ledger(paths.front_map_path("v5shape")))
    by_id = {line["id"]: line for line in lines}
    assert by_id[parent]["basis"] == "assumed"
    assert by_id[child]["basis"] == "assumed"
    assert by_id[child]["derived_from"] == [parent]


def test_map_add_unknown_front_and_unknown_from_are_refused(
        env, monkeypatch, capsys):
    """Unknown front and unknown --from are named; nothing is written."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(front="missing"), SUP) == 1
    _, err = capsys.readouterr()
    assert "missing" in err
    assert run(monkeypatch, add_argv(derived_from=["map-notreal"]), SUP) == 1
    _, err = capsys.readouterr()
    assert "map-notreal" in err
    assert store.read_ledger(paths.front_map_path("v5shape")) == []


def test_map_add_neither_basis_and_empty_fields_are_refused(
        env, monkeypatch, capsys):
    """Neither basis, empty section and empty fact are all named at once."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    argv = ["map", "add", "v5shape", "--repo", "foreman",
            "--section", "  ", "--fact", ""]
    assert run(monkeypatch, argv, SUP) == 1
    _, err = capsys.readouterr()
    assert "--section" in err
    assert "--fact" in err
    assert "seen" in err
    assert "assumed" in err
    assert store.read_ledger(paths.front_map_path("v5shape")) == []
