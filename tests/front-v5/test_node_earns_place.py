"""Decision 34: a node earns its place. The door refuses a node whose
verify command equals its parent's; a node with its own verify is admitted."""

from __future__ import annotations

import json
import subprocess

import pytest

from foreman import cli, fronts, paths
from foreman.caller import SESSION_ENV

PARENT_VERIFY = "PYTHONPATH=src python -m pytest tests -q"

V5_BRIEF = '''\
name = "places"
goal = "A front whose nodes each earn their place."
finish-line = "The door refuses a node that repeats its parent's verify."
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
work = "wplaces"
target = "main"
check = "true"
land = "push"
'''

NODE = ["--must-not-touch", "x", "--reason", "y", "--break", "z",
        "--repo", "foreman"]


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
    brief = tmp_path / "brief"
    brief.mkdir()
    (brief / "brief.toml").write_text(V5_BRIEF.format(url=bare),
                                      encoding="utf-8")
    assert fronts.front_add_main(str(brief)) == 0
    tree = paths.front_tree_path("places")
    tree.parent.mkdir(parents=True, exist_ok=True)
    tree.write_text(json.dumps({
        "id": "mil-1", "front": "places", "parent": "places",
        "kind": "milestone", "title": "one", "verify": PARENT_VERIFY,
        "must_not_touch": "x", "reason": "y", "break": "z",
        "repo": "foreman", "state": ""}) + "\n", encoding="utf-8")
    return tmp_path


def run(argv: list[str]) -> int:
    try:
        return cli.main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


@pytest.mark.xfail(strict=True,
                   reason="job-8.5a builds this; its worker removes this marker")
def test_a_node_repeating_its_parents_verify_is_refused(front, capsys):
    assert run(["node", "add", "places", "--parent", "mil-1", "--kind",
                "task", "--title", "echo", "--verify", PARENT_VERIFY,
                *NODE]) != 0
    err = capsys.readouterr().err
    assert "refused" in err and "verify" in err and "mil-1" in err


def test_a_node_with_its_own_verify_is_admitted(front):
    assert run(["node", "add", "places", "--parent", "mil-1", "--kind",
                "task", "--title", "its own",
                "--verify", "PYTHONPATH=src python -m pytest tests/x -q",
                *NODE]) == 0
