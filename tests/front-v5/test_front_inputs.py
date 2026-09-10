"""front add admits a v5-shape brief and leaves old briefs unchanged.

A brief is v5-shape when it carries ``goal``. These tests drive the real
``front add`` against a fresh FOREMAN_STATE, with a bare git repository
on tmp_path as the repository ``url`` (``main`` present, ``v5`` absent).
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from foreman import fronts, paths, store
from foreman.caller import SESSION_ENV

ROOT = Path(__file__).resolve().parents[2]

#: Keys an old-shape front line carried before this change, minus id/at/by
#: which the ledger stamps per write.
OLD_FRONT_KEYS = {
    "after", "allocation", "brief_path", "done_when", "fixture",
    "land_on", "merge", "monitors", "name", "order", "prefer",
    "reviews", "state", "supervisor", "want",
}


V5_BRIEF = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
  "Landing is a queued script.",
]
supervisor = "grok-4.6:high"
team = [
  "grok-4.6:high:1:supervisor",
  "grok-4.6:high:2:builder",
  "opus-5:high:1:backup-builder",
  "astra-6:low:1:reviewer",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5"
target = "main"
check = "python -m pytest tests -q"
land = "push"
trailers = ["Signed-off-by: Foreman"]
pr-body = "the front"
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


def make_bare(root: Path) -> tuple[Path, str]:
    """A bare repo with ``main`` and no ``v5``. Returns (path, main sha)."""
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


def write_v5(root: Path, name: str, url: str, text: str | None = None) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    body = V5_BRIEF.format(name=name, url=url) if text is None else text
    (brief_dir / "brief.toml").write_text(body, encoding="utf-8")
    return brief_dir


def add_refused(capsys, brief_dir: Path, *markers: str) -> str:
    rc = fronts.front_add_main(str(brief_dir))
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert err.count("refused:") == 1
    for marker in markers:
        assert marker in err, f"{marker!r} not named in: {err.strip()}"
    return err


def test_v5_brief_is_recorded(env):
    """A brief shaped like fronts/v5/FRONT.md writes every v5 field.

    Derived allocation is grok=2 (builder) + opus=1 (backup-builder) +
    astra=1 (reviewer). No task lines. base_sha is the bare repo's main.
    """
    bare, sha = make_bare(env)
    brief = write_v5(env, "v5shape", str(bare))
    assert fronts.front_add_main(str(brief)) == 0
    record = store.fold_by_id(
        store.read_ledger(paths.front_record_path("v5shape")))[0]
    assert record["shape"] == "v5"
    assert record["goal"] == (
        "Foreman starts a front from the owner's inputs alone.")
    assert record["finish_line"] == (
        "The front is admitted, recorded and old briefs still add.")
    assert record["decisions"] == [
        "Everything is a CLI verb.",
        "Landing is a queued script.",
    ]
    assert record["supervisor"] == {
        "agent": "grok-4.6",
        "pool": "grok",
        "model": "grok-4.6",
        "effort": "high",
    }
    assert record["team"] == [
        {"agent": "grok-4.6", "pool": "grok", "model": "grok-4.6",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "grok-4.6", "pool": "grok", "model": "grok-4.6",
         "effort": "high", "count": 2, "role": "builder"},
        {"agent": "opus-5", "pool": "claude", "model": "claude-opus-5",
         "effort": "high", "count": 1, "role": "backup-builder"},
        {"agent": "astra-6", "pool": "codex", "model": "gpt-6-astra",
         "effort": "low", "count": 1, "role": "reviewer"},
    ]
    assert record["repositories"] == [{
        "name": "foreman",
        "url": str(bare),
        "base": "main",
        "work": "v5",
        "target": "main",
        "check": "python -m pytest tests -q",
        "land": "push",
        "trailers": ["Signed-off-by: Foreman"],
        "pr_body": "the front",
        "base_sha": sha,
    }]
    assert record["want"] == record["goal"]
    assert record["done_when"] == record["finish_line"]
    assert record["land_on"] == "main"
    assert record["allocation"] == {"grok": 2, "opus": 1, "astra": 1}
    assert store.read_ledger(paths.front_tasks_path("v5shape")) == []


def test_v5_missing_finish_line_is_refused(env, capsys):
    bare, _sha = make_bare(env)
    text = V5_BRIEF.format(name="nofinish", url=str(bare)).replace(
        'finish-line = "The front is admitted, recorded and old briefs still add."\n',
        "")
    add_refused(capsys, write_v5(env, "nofinish", str(bare), text),
                "finish-line")
    assert not paths.front_record_path("nofinish").exists()


def test_v5_two_sentence_finish_line_is_refused(env, capsys):
    bare, _sha = make_bare(env)
    text = V5_BRIEF.format(name="twosent", url=str(bare)).replace(
        'finish-line = "The front is admitted, recorded and old briefs still add."',
        'finish-line = "It works. Everyone cheers."')
    add_refused(capsys, write_v5(env, "twosent", str(bare), text),
                "finish-line")
    assert not paths.front_record_path("twosent").exists()


def test_v5_brief_with_task_is_refused(env, capsys):
    bare, _sha = make_bare(env)
    text = V5_BRIEF.format(name="withtask", url=str(bare)) + '''
[[task]]
title = "a leftover task"
scope = """
WHAT: leftover.
INPUTS: none.
OUTPUTS: none.
OUT OF SCOPE: everything else.
"""
verify = "true"
size = 1
'''
    add_refused(capsys, write_v5(env, "withtask", str(bare), text),
                "task")
    assert not paths.front_record_path("withtask").exists()


def test_v5_unknown_agent_is_refused_naming_every_pool(env, capsys):
    """An agent that is not a pool name, model or alias lists every pool."""
    bare, _sha = make_bare(env)
    text = V5_BRIEF.format(name="noagent", url=str(bare)).replace(
        '"opus-5:high:1:backup-builder"',
        '"no-such-agent:high:1:backup-builder"')
    err = add_refused(capsys, write_v5(env, "noagent", str(bare), text),
                      "team", "no-such-agent",
                      "grok-4.6", "opus-5", "astra-6", "muse-spark")
    assert "model grok-4.6" in err
    assert "model claude-opus-5" in err
    assert "aliases opus-5, opus" in err
    assert "aliases astra-6, astra" in err
    assert not paths.front_record_path("noagent").exists()


def test_v5_work_branch_exists_is_refused(env, capsys):
    bare, _sha = make_bare(env)
    subprocess.run(["git", "-C", str(bare), "branch", "v5"], check=True)
    add_refused(capsys, write_v5(env, "haswork", str(bare)),
                "work", "exists on the remote")
    assert not paths.front_record_path("haswork").exists()


def test_v5_refusals_name_the_field(env, capsys):
    """The five defects the job names, each refused naming its field."""
    bare, _sha = make_bare(env)
    missing = V5_BRIEF.format(name="nofinish", url=str(bare)).replace(
        'finish-line = "The front is admitted, recorded and old briefs still add."\n',
        "")
    add_refused(capsys, write_v5(env, "nofinish", str(bare), missing),
                "finish-line")

    two = V5_BRIEF.format(name="twosent", url=str(bare)).replace(
        'finish-line = "The front is admitted, recorded and old briefs still add."',
        'finish-line = "It works. Everyone cheers."')
    add_refused(capsys, write_v5(env, "twosent", str(bare), two),
                "finish-line")

    unknown = V5_BRIEF.format(name="noagent", url=str(bare)).replace(
        '"opus-5:high:1:backup-builder"',
        '"no-such-agent:high:1:backup-builder"')
    add_refused(capsys, write_v5(env, "noagent", str(bare), unknown),
                "team", "no-such-agent")

    occupied = env / "occupied.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(bare), str(occupied)],
                   check=True)
    subprocess.run(["git", "-C", str(occupied), "branch", "v5"], check=True)
    add_refused(capsys, write_v5(env, "haswork", str(occupied)),
                "work", "exists on the remote")

    leftover = V5_BRIEF.format(name="withtask", url=str(bare)) + '''
[[task]]
title = "a leftover task"
scope = """
WHAT: leftover.
INPUTS: none.
OUTPUTS: none.
OUT OF SCOPE: everything else.
"""
verify = "true"
size = 1
'''
    add_refused(capsys, write_v5(env, "withtask", str(bare), leftover),
                "task")


def test_existing_briefs_keep_their_front_line(env):
    """Every brief under briefs/ still admits with the pre-v5 front line.

    ``runtime`` predates the one-sentence ``done-when`` rule and is
    recorded ``--closed`` the way a historical front is; every other
    brief is a live add.
    """
    by_name: dict[str, tuple[Path, dict]] = {}
    for directory in sorted((ROOT / "briefs").iterdir()):
        path = directory / "brief.toml"
        if not path.is_file():
            continue
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        by_name[data["name"]] = (directory, data)
    pending = set(by_name)
    added: set[str] = set()
    while pending:
        ready = [
            name for name in pending
            if all(dep in added for dep in (by_name[name][1].get("after") or []))
        ]
        assert ready, f"unresolved after among {sorted(pending)}"
        for name in sorted(ready):
            directory, data = by_name[name]
            closed = name == "runtime"
            assert fronts.front_add_main(
                str(directory), closed=closed) == 0, name
            record = store.fold_by_id(
                store.read_ledger(paths.front_record_path(name)))[0]
            got = {key: value for key, value in record.items()
                   if key not in ("id", "at", "by", "build")}
            assert set(got) == OLD_FRONT_KEYS, name
            assert got["name"] == name
            assert got["want"] == (data.get("want") or "").strip()
            assert got["done_when"] == (data.get("done-when") or "").strip()
            assert got["land_on"] == (data.get("land-on") or "")
            assert got["allocation"] == dict(data.get("allocation") or {})
            assert got["after"] == list(data.get("after") or [])
            assert got["supervisor"] is None
            assert got["state"] == ("done" if closed else "queued")
            tasks = store.read_ledger(paths.front_tasks_path(name))
            if closed:
                assert tasks == []
            else:
                assert len(tasks) == len(data.get("task") or []), name
            added.add(name)
            pending.remove(name)
