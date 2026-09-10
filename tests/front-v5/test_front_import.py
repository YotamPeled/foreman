"""front import reads map.md, milestones.jsonl and tree.jsonl through the doors.

Every test drives the real CLI against a fresh FOREMAN_STATE with a
v5-shape front named ``v5`` whose repository is named ``foreman``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, mcp as mcp_module, paths, store
from foreman.caller import SESSION_ENV

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
WORKER = "ses-wrk0001"
ROOT = Path(__file__).resolve().parents[2]
V5_DIR = ROOT / "fronts" / "v5"
SEEN_AT = "2026-09-10T23:00:00+00:00"
COMMIT = "2058289403fe5cfaaee52db50ba1da79337826dd"

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


def add_front(env: Path, monkeypatch, capsys, name: str = "v5") -> str:
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


def copy_v5(dest: Path) -> Path:
    shutil.copytree(V5_DIR, dest)
    return dest


def import_argv(directory, dry_run=False):
    argv = ["front", "import", "v5", str(directory),
            "--seen-at", SEEN_AT, "--commit", COMMIT]
    if dry_run:
        argv.append("--dry-run")
    return argv


def summary_lines(text: str) -> list[str]:
    return [line for line in text.strip().splitlines() if line]


def parse_summary(line: str) -> tuple[str, int, int, int]:
    name, rest = line.split(":", 1)
    parts = rest.strip().split(",")
    new = int(parts[0].strip().split()[0])
    present = int(parts[1].strip().split()[0])
    revised = int(parts[2].strip().split()[0])
    return name.strip(), new, present, revised


def ids_in_jsonl(path: Path) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        record = json.loads(raw)
        nid = record.get("id")
        if isinstance(nid, str) and nid not in seen:
            seen.add(nid)
            ids.append(nid)
    return ids


def test_front_import_of_v5_files_is_idempotent(env, monkeypatch, capsys):
    """A copy of fronts/v5/ imports; a second import adds nothing."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5")
    directory = copy_v5(env / "v5files")
    assert run(monkeypatch, import_argv(directory), SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    lines = summary_lines(out)
    assert len(lines) == 3
    names = [parse_summary(line)[0] for line in lines]
    assert names == ["milestones.jsonl", "tree.jsonl", "map.md"]
    first = {name: (new, present, revised)
             for name, new, present, revised in (parse_summary(line)
                                                 for line in lines)}
    assert first["milestones.jsonl"][0] == 7
    assert first["tree.jsonl"][0] > 0
    assert first["map.md"][0] > 0

    assert run(monkeypatch, ["milestone", "list", "v5", "--json"], SUP) == 0
    listed, err = capsys.readouterr()
    assert err == ""
    live = json.loads(listed)["milestones"]
    assert len(live) == 7
    assert {item["id"] for item in live} == set(ids_in_jsonl(
        directory / "milestones.jsonl"))

    assert run(monkeypatch, ["node", "list", "v5", "--json"], SUP) == 0
    listed, err = capsys.readouterr()
    assert err == ""
    folded = json.loads(listed)
    assert {item["id"] for item in folded} == set(ids_in_jsonl(
        directory / "tree.jsonl"))

    mil_before = store.read_ledger(paths.front_milestones_path("v5"))
    tree_before = store.read_ledger(paths.front_tree_path("v5"))
    map_before = store.read_ledger(paths.front_map_path("v5"))
    assert run(monkeypatch, import_argv(directory), SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    second = [parse_summary(line) for line in summary_lines(out)]
    assert [item[0] for item in second] == names
    for _name, new, _present, _revised in second:
        assert new == 0
    assert store.read_ledger(paths.front_milestones_path("v5")) == mil_before
    assert store.read_ledger(paths.front_tree_path("v5")) == tree_before
    assert store.read_ledger(paths.front_map_path("v5")) == map_before


def test_front_import_stops_on_a_job_parent(env, monkeypatch, capsys):
    """A tree line whose parent is a job names tree.jsonl and the line."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5")
    directory = copy_v5(env / "v5files")
    tree_path = directory / "tree.jsonl"
    rows = tree_path.read_text(encoding="utf-8").splitlines()
    lineno = 108
    record = json.loads(rows[lineno - 1])
    assert record["id"] == "tsk-7.4"
    record["parent"] = "job-1.0a"
    rows[lineno - 1] = json.dumps(record)
    tree_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    assert run(monkeypatch, import_argv(directory), SUP) == 1
    _out, err = capsys.readouterr()
    assert "tree.jsonl" in err
    assert str(lineno) in err
    assert "job" in err
    folded = store.fold_by_id(store.read_ledger(paths.front_tree_path("v5")))
    by_id = {item["id"]: item for item in folded}
    assert "tsk-7.4" not in by_id
    assert "job-1.0a" in by_id
    assert "mil-1" in by_id
    earlier = ids_in_jsonl(V5_DIR / "tree.jsonl")
    applied = earlier[:earlier.index("tsk-7.4")]
    assert set(applied) <= set(by_id)
    assert len(store.read_ledger(paths.front_tree_path("v5"))) == lineno - 1
    assert len(store.fold_by_id(
        store.read_ledger(paths.front_milestones_path("v5")))) == 7


def test_front_import_dry_run_writes_nothing(env, monkeypatch, capsys):
    """--dry-run prints the three counts and leaves every ledger empty."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5")
    directory = copy_v5(env / "v5files")
    assert run(monkeypatch, import_argv(directory, dry_run=True), SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    lines = summary_lines(out)
    assert len(lines) == 3
    parsed = [parse_summary(line) for line in lines]
    assert [item[0] for item in parsed] == [
        "milestones.jsonl", "tree.jsonl", "map.md"]
    assert parsed[0][1] == 7
    assert parsed[1][1] > 0
    assert parsed[2][1] > 0
    assert store.read_ledger(paths.front_milestones_path("v5")) == []
    assert store.read_ledger(paths.front_tree_path("v5")) == []
    assert store.read_ledger(paths.front_map_path("v5")) == []


def test_front_import_requires_seen_at_when_map_exists(
        env, monkeypatch, capsys):
    """map.md without --seen-at/--commit is refused and writes nothing."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5")
    directory = copy_v5(env / "v5files")
    assert run(monkeypatch, ["front", "import", "v5", str(directory)],
               SUP) == 1
    _out, err = capsys.readouterr()
    assert "--seen-at" in err
    assert "--commit" in err
    assert store.read_ledger(paths.front_milestones_path("v5")) == []
    assert store.read_ledger(paths.front_tree_path("v5")) == []
    assert store.read_ledger(paths.front_map_path("v5")) == []


def test_front_import_tool_is_listed_for_supervisor_not_worker(
        env, monkeypatch):
    """front_import is a supervisor tool and is hidden from a worker session."""
    seed_roster(
        session_entry(SUP, "supervisor", "v5"),
        session_entry(WORKER, "muse", "v5"),
    )
    monkeypatch.setenv(SESSION_ENV, SUP)
    supervisor = {tool["name"] for tool in
                  mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list",
                                     "params": {}})["result"]["tools"]}
    assert "front_import" in supervisor
    monkeypatch.setenv(SESSION_ENV, WORKER)
    worker = {tool["name"] for tool in
              mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list",
                                 "params": {}})["result"]["tools"]}
    assert "front_import" not in worker
