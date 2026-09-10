"""front add --adopt upgrades a running old-shape front to v5 shape in place.

The break this catches: an adopted record without ``repositories`` (or
without the team) leaves a queued builder job with no pool and no
repository, so the runtime launches it outside any git repository.
Expected values come from the brief this test writes, not from the code.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from foreman import fronts, launch, paths, store
from foreman.caller import SESSION_ENV

OLD_BRIEF = '''name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
grok = 2

[[task]]
title = "first"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "true"
size = 1
after = []
'''

V5_BRIEF = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
  "Landing is a queued script.",
]
supervisor = "opus-5:high"
team = [
  "opus-5:high:1:supervisor",
  "muse-spark:xhigh:3:builder",
  "opus-5:high:1:backup-builder",
  "astra-6:low:1:reviewer",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "work"
target = "main"
check = "true"
land = "push"
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    git("init", "-b", "main", "-q", str(tmp_path))
    git("-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init")
    return tmp_path


def git(*args: str) -> None:
    subprocess.run(["git", "-c", "user.email=test@example.invalid",
                    "-c", "user.name=foreman-test", *args],
                   check=True, capture_output=True)


def bare_with_work_branch(root: Path) -> Path:
    """A bare remote holding main and the front's own work branch."""
    src = root / "src-repo"
    src.mkdir()
    git("init", "-b", "main", "-q", str(src))
    git("-C", str(src), "commit", "-q", "--allow-empty", "-m", "init")
    git("-C", str(src), "branch", "work")
    bare = root / "remote.git"
    git("clone", "--bare", "-q", str(src), str(bare))
    return bare


def write_brief(root: Path, sub: str, text: str) -> Path:
    brief_dir = root / sub
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(text, encoding="utf-8")
    return brief_dir


def old_front_with_queued_job(root: Path, name: str = "old1") -> dict:
    assert fronts.front_add_main(
        str(write_brief(root, "old-brief", OLD_BRIEF.format(name=name)))) == 0
    tree = paths.front_tree_path(name)
    tree.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": "mil-1", "front": name, "parent": name, "kind": "milestone",
         "title": "one", "state": ""},
        {"id": "job-1", "front": name, "parent": "mil-1", "kind": "job",
         "title": "leaf", "role": "builder", "repo": "foreman",
         "verify": "true", "must_not_touch": "x", "reason": "y",
         "break": "z", "state": "queued"},
    ]
    with tree.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return fronts.read_front_record(name)


def job_node(name: str) -> dict:
    rows = store.fold_by_id(store.read_ledger(paths.front_tree_path(name)))
    return next(row for row in rows if row.get("id") == "job-1")


def test_adopt_upgrades_the_record_and_keeps_its_identity(env):
    before = old_front_with_queued_job(env)
    tasks_before = store.read_ledger(paths.front_tasks_path("old1"))
    bare = bare_with_work_branch(env)
    brief = write_brief(env, "v5-brief", V5_BRIEF.format(name="old1", url=bare))

    assert fronts.front_add_main(str(brief), adopt=True) == 0

    after = fronts.read_front_record("old1")
    assert after["shape"] == "v5"
    assert after["id"] == before["id"]
    assert after.get("supervisor") == before.get("supervisor")
    assert after["state"] == before["state"]
    assert after["goal"] == "Foreman starts a front from the owner's inputs alone."
    repos = after["repositories"]
    assert [r["name"] for r in repos] == ["foreman"]
    assert repos[0]["work"] == "work" and repos[0]["target"] == "main"
    assert store.read_ledger(paths.front_tasks_path("old1")) == tasks_before


def test_adopted_front_gives_a_queued_builder_a_pool_and_a_repository(env):
    old_front_with_queued_job(env)
    node = job_node("old1")
    record = fronts.read_front_record("old1")
    assert launch._team_pool(record, node) is None  # the gap being fixed
    bare = bare_with_work_branch(env)
    brief = write_brief(env, "v5-brief", V5_BRIEF.format(name="old1", url=bare))

    assert fronts.front_add_main(str(brief), adopt=True) == 0

    record = fronts.read_front_record("old1")
    assert launch._team_pool(record, node) == "muse"
    assert launch._queued_repo_path(record, node) == str(bare)
    assert launch._queued_base_branch(record, node) == "work"


def test_adopt_refuses_an_unknown_front(env, capsys):
    bare = bare_with_work_branch(env)
    brief = write_brief(env, "v5-brief", V5_BRIEF.format(name="nosuch", url=bare))
    assert fronts.front_add_main(str(brief), adopt=True) != 0
    assert "nosuch" in capsys.readouterr().err


def test_adopt_refuses_a_front_already_in_v5_shape(env, capsys):
    old_front_with_queued_job(env)
    bare = bare_with_work_branch(env)
    brief = write_brief(env, "v5-brief", V5_BRIEF.format(name="old1", url=bare))
    assert fronts.front_add_main(str(brief), adopt=True) == 0
    capsys.readouterr()
    assert fronts.front_add_main(str(brief), adopt=True) != 0
    assert "v5" in capsys.readouterr().err


def test_plain_front_add_still_refuses_an_existing_name(env, capsys):
    old_front_with_queued_job(env)
    bare = bare_with_work_branch(env)
    brief = write_brief(env, "v5-brief", V5_BRIEF.format(name="old1", url=bare))
    assert fronts.front_add_main(str(brief)) != 0
    assert "already on the ledger" in capsys.readouterr().err
