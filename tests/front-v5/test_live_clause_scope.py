"""The live finish-line clauses f2, f3 and f4 read only --front's lines.

A front that has queued nothing, landed nothing and holds no tree must
fail all three, however many words like "landed", "queue:" or "tree:"
other lines of the screen carry. The proof runs this checkout's module
against a state directory the test owns, never the installed binary.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from foreman import fronts
from foreman.caller import SESSION_ENV

REPO = Path(__file__).resolve().parents[2]
PROOF = REPO / "bin" / "foreman-proof"

V5_BRIEF = '''\
name = "quiet"
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
work = "wquiet"
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
    brief = tmp_path / "brief-quiet"
    brief.mkdir()
    (brief / "brief.toml").write_text(V5_BRIEF.format(url=bare),
                                      encoding="utf-8")
    assert fronts.front_add_main(str(brief)) == 0
    config.mkdir(parents=True, exist_ok=True)
    return state, config


def _without_installed_foreman(path: str) -> str:
    return os.pathsep.join(
        part for part in path.split(os.pathsep)
        if part and not os.access(os.path.join(part, "foreman"), os.X_OK))


def _live(state: Path, config: Path, front: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("FOREMAN_SESSION", "FOREMAN_STATE", "FOREMAN_CONFIG")}
    env.update(FOREMAN_STATE=str(state), FOREMAN_CONFIG=str(config),
               PATH=_without_installed_foreman(env.get("PATH", "")),
               PYTHONPATH=str(REPO / "src"))
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


@pytest.mark.xfail(strict=True, reason="job-7.3c builds this; its worker removes this marker")
@pytest.mark.parametrize("clause", ["f2", "f3", "f4"])
def test_a_front_with_nothing_fails_each_live_clause_naming_it(
        quiet_front, clause):
    state, config = quiet_front
    result = _live(state, config, "quiet")
    assert clause in result, result
    assert result[clause].startswith("FAIL"), result[clause]
    assert "quiet" in result[clause], result[clause]
