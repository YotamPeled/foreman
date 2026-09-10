"""Decision 39: `job edit` changes a queued job's worker (role, pool,
effort), is refused once the job has started, and records who and why."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from foreman import cli, fronts, paths, store
from foreman.caller import SESSION_ENV

V5_BRIEF = '''\
name = "edits"
goal = "A front whose queued jobs change worker."
finish-line = "A queued job moves to another pool by one verb."
decisions = ["Everything is a CLI verb."]
supervisor = "opus-5:high"
team = [
  "opus-5:high:1:supervisor",
  "grok-4.6:high:2:builder",
  "opus-5:high:1:backup-builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "wedits"
target = "main"
check = "true"
land = "push"
'''


def git(*args: str) -> None:
    subprocess.run(["git", "-c", "user.email=test@example.invalid",
                    "-c", "user.name=foreman-test", *args],
                   check=True, capture_output=True)


def _node(node_id: str, state: str) -> dict:
    return {"id": node_id, "front": "edits", "parent": "mil-1",
            "kind": "job", "title": node_id, "role": "builder",
            "repo": "foreman", "verify": "true", "must_not_touch": "x",
            "reason": "y", "break": "z", "state": state}


@pytest.fixture()
def front(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    git("init", "-b", "main", "-q", str(tmp_path))
    git("-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init")
    src = tmp_path / "src-repo"
    src.mkdir()
    git("init", "-b", "main", "-q", str(src))
    git("-C", str(src), "commit", "-q", "--allow-empty", "-m", "init")
    bare = tmp_path / "remote.git"
    git("clone", "--bare", "-q", str(src), str(bare))
    brief = tmp_path / "brief"
    brief.mkdir()
    (brief / "brief.toml").write_text(V5_BRIEF.format(url=bare),
                                      encoding="utf-8")
    assert fronts.front_add_main(str(brief)) == 0
    tree = paths.front_tree_path("edits")
    tree.parent.mkdir(parents=True, exist_ok=True)
    with tree.open("a", encoding="utf-8") as handle:
        for row in ({"id": "mil-1", "front": "edits", "parent": "edits",
                     "kind": "milestone", "title": "one", "state": ""},
                    _node("job-1", "queued"), _node("job-2", "running")):
            handle.write(json.dumps(row) + "\n")
    return tmp_path


BUILT_BY_8_2A = pytest.mark.xfail(
    strict=True, reason="job-8.2a builds this; its worker removes this marker")


def run(argv: list[str]) -> int:
    try:
        return cli.main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def lines_for(node_id: str) -> list[dict]:
    return [row for row in store.read_ledger(paths.front_tree_path("edits"))
            if row.get("id") == node_id]


@BUILT_BY_8_2A
def test_edit_moves_a_queued_job_to_another_role_pool_and_effort(front):
    assert run(["job", "edit", "edits", "job-1", "--role", "backup-builder",
                "--pool", "claude", "--effort", "xhigh",
                "--reason", "the builder pool is out"]) == 0
    folded = store.fold_by_id(lines_for("job-1"))[-1]
    assert folded["role"] == "backup-builder"
    assert folded["pool"] == "claude"
    assert folded["effort"] == "xhigh"
    last = lines_for("job-1")[-1]
    assert last.get("by")
    assert "the builder pool is out" in json.dumps(last)


@BUILT_BY_8_2A
def test_edit_of_a_started_job_is_refused(front, capsys):
    before = len(lines_for("job-2"))
    assert run(["job", "edit", "edits", "job-2", "--role", "backup-builder",
                "--reason", "too late"]) != 0
    assert "job-2 is running" in capsys.readouterr().err
    assert len(lines_for("job-2")) == before


@BUILT_BY_8_2A
def test_edit_refuses_an_unknown_pool_naming_it(front, capsys):
    assert run(["job", "edit", "edits", "job-1", "--pool", "nosuch",
                "--reason", "try"]) != 0
    err = capsys.readouterr().err
    assert "refused" in err and "nosuch" in err


def test_existing_flags_still_edit(front):
    assert run(["job", "edit", "edits", "job-1", "--verify", "false"]) == 0
    assert store.fold_by_id(lines_for("job-1"))[-1]["verify"] == "false"
