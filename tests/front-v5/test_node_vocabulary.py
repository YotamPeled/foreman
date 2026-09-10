"""Decision 35: the tree speaks the design's language. The kind "task" is
renamed "node" in verbs, records and screens, with a migration for
existing fronts; "task" survives only where an old-shape front's record
needs reading."""

from __future__ import annotations

import json
import subprocess

import pytest

from foreman import cli, fronts, paths, store
from foreman.caller import SESSION_ENV

V5_BRIEF = '''\
name = "words"
goal = "A front whose tree says node."
finish-line = "Milestones, nodes, and jobs at the bottom."
decisions = ["Everything is a CLI verb."]
supervisor = "opus-5:high"
team = [
  "opus-5:high:1:supervisor",
  "opus-5:high:1:builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "wwords"
target = "main"
check = "true"
land = "push"
'''

OLD_BRIEF = '''name      = "oldwords"
order     = 1
want      = "An old front whose record still says task."
done-when = "It still reads."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
opus = 1

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

NODE = ["--verify", "PYTHONPATH=src python -m pytest tests/y -q",
        "--must-not-touch", "x", "--reason", "y", "--break", "z",
        "--repo", "foreman"]

BUILT_BY_8_6A = pytest.mark.xfail(
    strict=True, reason="job-8.6a builds this; its worker removes this marker")


def git(*args: str) -> None:
    subprocess.run(["git", "-c", "user.email=test@example.invalid",
                    "-c", "user.name=foreman-test", *args],
                   check=True, capture_output=True)


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
    for sub, text in (("brief", V5_BRIEF.format(url=bare)),
                      ("old-brief", OLD_BRIEF)):
        brief = tmp_path / sub
        brief.mkdir()
        (brief / "brief.toml").write_text(text, encoding="utf-8")
        assert fronts.front_add_main(str(brief)) == 0
    tree = paths.front_tree_path("words")
    tree.parent.mkdir(parents=True, exist_ok=True)
    with tree.open("a", encoding="utf-8") as handle:
        for row in (
                {"id": "mil-1", "front": "words", "parent": "words",
                 "kind": "milestone", "title": "one", "state": "",
                 "verify": "PYTHONPATH=src python -m pytest tests -q"},
                {"id": "tsk-1", "front": "words", "parent": "mil-1",
                 "kind": "task", "title": "a middle node", "state": "",
                 "verify": "PYTHONPATH=src python -m pytest tests/x -q"}):
            handle.write(json.dumps(row) + "\n")
    return tmp_path


def run(argv: list[str]) -> int:
    try:
        return cli.main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


@BUILT_BY_8_6A
def test_node_add_takes_kind_node(front):
    assert run(["node", "add", "words", "--parent", "mil-1", "--kind",
                "node", "--title", "a new middle node", *NODE]) == 0


@BUILT_BY_8_6A
def test_the_migration_renames_an_existing_task_node(front, capsys):
    assert run(["migrate"]) == 0
    capsys.readouterr()
    assert run(["node", "list", "words"]) == 0
    listing = capsys.readouterr().out
    line = next(row for row in listing.splitlines() if "tsk-1" in row)
    assert "  node  " in line and "  task  " not in line, listing
    folded = {row["id"]: row for row in store.fold_by_id(
        store.read_ledger(paths.front_tree_path("words")))}
    assert folded["tsk-1"]["kind"] == "node"


def test_an_old_shape_front_record_still_reads(front, capsys):
    assert run(["front", "show", "oldwords"]) == 0
    shown = capsys.readouterr().out
    assert "old-shape brief" in shown and '"first"' in shown, shown
