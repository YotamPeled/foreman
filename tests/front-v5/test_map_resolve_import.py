"""map resolve records a remote sha; map import reads a map.md.

Every test drives the real CLI against a fresh FOREMAN_STATE with a
v5-shape front whose repository url is a bare repo on tmp_path.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, mcp as mcp_module, paths, store
from foreman.caller import SESSION_ENV

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
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

OLD_BRIEF = '''\
name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 2

[[task]]
title = "first work"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "make check first"
size = 3
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


def make_bare(root: Path) -> tuple[Path, str]:
    src = root / "src-repo"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    bare = root / "remote.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src), str(bare)],
                   check=True)
    sha = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "main"],
        check=True, capture_output=True, text=True).stdout.strip()
    return bare, sha


def write_v5(root: Path, name: str, url: str) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=url), encoding="utf-8")
    return brief_dir


def add_front(env: Path, monkeypatch, capsys, name: str = "v5shape") -> tuple[Path, str]:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    bare, sha = make_bare(env)
    assert cli.main(["front", "add", str(write_v5(env, name, str(bare)))]) == 0
    capsys.readouterr()
    return bare, sha


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


def test_map_resolve_records_remote_sha(env, monkeypatch, capsys):
    """--ref main appends a seen fact whose refs[0].sha is the remote main."""
    bare, sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, ["map", "resolve", "v5shape",
                             "--repo", "foreman", "--ref", "main"],
               SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == f"foreman main {sha}"
    lines = store.read_ledger(paths.front_map_path("v5shape"))
    assert len(lines) == 1
    line = lines[0]
    assert line["section"] == "refs"
    assert line["text"] == f"origin/main = {sha}"
    assert line["basis"] == "seen"
    assert line["refs"][0]["sha"] == sha
    assert line["refs"][0]["sha"] == subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "main"],
        check=True, capture_output=True, text=True).stdout.strip()
    assert line["repo"] == "foreman"


def test_map_resolve_absent_ref_names_the_url(env, monkeypatch, capsys):
    """--ref nope is refused naming the repository url and writes nothing."""
    bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, ["map", "resolve", "v5shape",
                             "--repo", "foreman", "--ref", "nope"],
               SUP) == 1
    _, err = capsys.readouterr()
    assert "nope" in err
    assert str(bare) in err
    assert store.read_ledger(paths.front_map_path("v5shape")) == []


def test_map_resolve_unknown_repo_is_refused(env, monkeypatch, capsys):
    """--repo other names the known repositories and writes nothing."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, ["map", "resolve", "v5shape",
                             "--repo", "other", "--ref", "main"],
               SUP) == 1
    _, err = capsys.readouterr()
    assert "other" in err
    assert "foreman" in err
    assert store.read_ledger(paths.front_map_path("v5shape")) == []


def test_map_resolve_non_v5_front_is_refused(env, monkeypatch, capsys):
    """An old-shape front is refused as not v5-shape and writes nothing."""
    brief_dir = env / "oldshape"
    brief_dir.mkdir()
    (brief_dir / "brief.toml").write_text(
        OLD_BRIEF.format(name="oldshape"), encoding="utf-8")
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["front", "add", str(brief_dir)]) == 0
    capsys.readouterr()
    seed_supervisor("oldshape")
    assert run(monkeypatch, ["map", "resolve", "oldshape",
                             "--repo", "foreman", "--ref", "main"],
               SUP) == 1
    _, err = capsys.readouterr()
    assert "v5-shape" in err
    assert store.read_ledger(paths.front_map_path("oldshape")) == []


ROOT = Path(__file__).resolve().parents[2]
SEEN_AT = "2026-09-10T23:00:00+00:00"
COMMIT = "2058289403fe5cfaaee52db50ba1da79337826dd"


def copy_map_md(dest: Path) -> Path:
    """A copy of fronts/v5/map.md with the repository renamed to the fixture."""
    src = (ROOT / "fronts" / "v5" / "map.md").read_text(encoding="utf-8")
    renamed = re.sub(r"(## Repository:\s+)\S+", r"\1foreman", src, count=1)
    dest.write_text(renamed, encoding="utf-8")
    return dest


def import_argv(front, path, seen_at=SEEN_AT, commit=COMMIT):
    return ["map", "import", front, str(path),
            "--seen-at", seen_at, "--commit", commit]


def test_map_import_reads_map_md_and_is_idempotent(env, monkeypatch, capsys):
    """A renamed copy of fronts/v5/map.md imports; a second import adds 0."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    path = copy_map_md(env / "map.md")
    assert run(monkeypatch, import_argv("v5shape", path), SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    first = out.strip()
    assert first.startswith("imported ")
    assert "already present" in first
    imported_n = int(first.split()[1])
    assert imported_n > 0
    assert first == f"imported {imported_n} facts, 0 already present"
    assert run(monkeypatch, ["map", "show", "v5shape"], SUP) == 0
    shown, err = capsys.readouterr()
    assert err == ""
    assert "Repository: foreman" in shown
    assert "The runtime today, by area" in shown
    assert "The machine" in shown
    assert "Gaps between today and each decision" in shown
    assert "Assumptions I am making" in shown
    assert "- [seen]" in shown
    assert "- [assumed]" in shown
    assert "`store.py` is the one writer" in shown
    assert run(monkeypatch, import_argv("v5shape", path), SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == f"imported 0 facts, {imported_n} already present"
    after = store.read_ledger(paths.front_map_path("v5shape"))
    assert len(after) == imported_n


def test_map_import_untagged_paragraph_is_refused(env, monkeypatch, capsys):
    """A file with one untagged paragraph names its line and appends nothing."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    path = env / "untagged.md"
    path.write_text(
        "## Repository: foreman\n"
        "\n"
        "an untagged paragraph about storage\n",
        encoding="utf-8")
    assert run(monkeypatch, import_argv("v5shape", path), SUP) == 1
    _, err = capsys.readouterr()
    assert "3" in err
    assert "seen" in err
    assert "assumed" in err
    assert store.read_ledger(paths.front_map_path("v5shape")) == []


def test_map_import_tool_is_listed_for_supervisor_not_worker(
        env, monkeypatch):
    """map_import is a supervisor tool and is hidden from a worker session."""
    seed_roster(
        session_entry(SUP, "supervisor", "v5shape"),
        session_entry(WORKER, "muse", "v5shape"),
    )
    monkeypatch.setenv(SESSION_ENV, SUP)
    supervisor = {tool["name"] for tool in
                  mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list",
                                     "params": {}})["result"]["tools"]}
    assert "map_import" in supervisor
    monkeypatch.setenv(SESSION_ENV, WORKER)
    worker = {tool["name"] for tool in
              mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list",
                                 "params": {}})["result"]["tools"]}
    assert "map_import" not in worker


def test_map_resolve_tool_is_listed_for_supervisor_not_worker(
        env, monkeypatch):
    """map_resolve is a supervisor tool and is hidden from a worker session."""
    seed_roster(
        session_entry(SUP, "supervisor", "v5shape"),
        session_entry(WORKER, "muse", "v5shape"),
    )
    monkeypatch.setenv(SESSION_ENV, SUP)
    supervisor = {tool["name"] for tool in
                  mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list",
                                     "params": {}})["result"]["tools"]}
    assert "map_resolve" in supervisor
    monkeypatch.setenv(SESSION_ENV, WORKER)
    worker = {tool["name"] for tool in
              mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list",
                                 "params": {}})["result"]["tools"]}
    assert "map_resolve" not in worker
