"""The live finish-line clauses f2, f3 and f4 read only --front's lines.

A front that has queued nothing, landed nothing and holds no tree must
fail all three while another front on the same screen carries every word
the checks look for ("landed", "queue:", "reserved", "pieces", "split
from", "tree:"). The proof runs this checkout's module
against a state directory the test owns, never the installed binary.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import json

from foreman import fronts, paths
from foreman.caller import SESSION_ENV

REPO = Path(__file__).resolve().parents[2]
PROOF = REPO / "bin" / "foreman-proof"

V5_BRIEF = '''\
name = "{name}"
goal = "A front that has done nothing yet."
finish-line = "Nothing is queued, nothing has landed."
decisions = ["Everything is a CLI verb."]
supervisor = "opus-5:high"
team = [
  "opus-5:high:1:supervisor",
  "muse-spark:xhigh:1:builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "w{name}"
target = "main"
check = "true"
land = "push"
'''


def git(*args: str) -> None:
    subprocess.run(["git", "-c", "user.email=test@example.invalid",
                    "-c", "user.name=foreman-test", *args],
                   check=True, capture_output=True)


@pytest.fixture()
def quiet_front(tmp_path, monkeypatch):
    state, config = tmp_path / "state", tmp_path / "config"
    monkeypatch.setenv("FOREMAN_STATE", str(state))
    monkeypatch.setenv("FOREMAN_CONFIG", str(config))
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
    for name in ("busy", "quiet"):
        brief = tmp_path / f"brief-{name}"
        brief.mkdir()
        (brief / "brief.toml").write_text(
            V5_BRIEF.format(name=name, url=bare), encoding="utf-8")
        assert fronts.front_add_main(str(brief)) == 0
    _busy_plan()
    created, violations = fronts.reserve_front(
        "busy", fronts.read_front_record("busy"), "builders", None)
    assert created and not violations, violations
    config.mkdir(parents=True, exist_ok=True)
    return state, config


def _append(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _busy_plan() -> None:
    """Front busy holds a milestone and a tree whose one job has landed."""
    _append(paths.front_milestones_path("busy"), [
        {"id": "mil-1", "front": "busy", "op": "add", "order": 1,
         "title": "one", "done_when": "the leaf landed"}])
    _append(paths.front_tree_path("busy"), [
        {"id": "mil-1", "front": "busy", "parent": "busy",
         "kind": "milestone", "title": "one", "state": ""},
        {"id": "job-b1", "front": "busy", "parent": "mil-1", "kind": "job",
         "title": "leaf", "role": "builder", "repo": "foreman",
         "verify": "true", "must_not_touch": "x", "reason": "y",
         "break": "z", "state": "landed"}])


def _without_installed_foreman(path: str) -> str:
    return os.pathsep.join(
        part for part in path.split(os.pathsep)
        if part and not os.access(os.path.join(part, "foreman"), os.X_OK))


def _env(state: Path, config: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("FOREMAN_SESSION", "FOREMAN_STATE", "FOREMAN_CONFIG")}
    env.update(FOREMAN_STATE=str(state), FOREMAN_CONFIG=str(config),
               PATH=_without_installed_foreman(env.get("PATH", "")),
               PYTHONPATH=str(REPO / "src"))
    return env


def _live(state: Path, config: Path, front: str) -> dict[str, str]:
    env = _env(state, config)
    proc = subprocess.run(
        [sys.executable, str(PROOF), "v5", "--live", "--front", front],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)
    lines = {}
    for line in proc.stdout.splitlines():
        match = re.match(r"^CLAUSE (f\d) (PASS|FAIL|SKIP) ?(.*)$", line)
        if match:
            lines[match.group(1)] = f"{match.group(2)} {match.group(3)}"
    assert "live: read-only" in proc.stdout, proc.stdout + proc.stderr
    return lines


_BUILT_BY_7_3C = pytest.mark.xfail(
    strict=True, reason="job-7.3c builds this; its worker removes this marker")


@pytest.mark.parametrize("clause", [
    pytest.param("f2", marks=_BUILT_BY_7_3C),
    "f3",
    pytest.param("f4", marks=_BUILT_BY_7_3C),
])
def test_a_front_with_nothing_fails_each_live_clause_naming_it(
        quiet_front, clause):
    state, config = quiet_front
    screen = subprocess.run(
        [sys.executable, "-m", "foreman", "status"], cwd=str(REPO),
        env=_env(state, config), capture_output=True, text=True,
        timeout=120).stdout
    for word in ("landed", "queue:", "reserved", "pieces", "split from",
                 "tree:"):
        assert word in screen, f"the screen lacks {word!r}:\n{screen}"
    result = _live(state, config, "quiet")
    assert clause in result, result
    assert result[clause].startswith("FAIL"), result[clause]
    assert "quiet" in result[clause], result[clause]
