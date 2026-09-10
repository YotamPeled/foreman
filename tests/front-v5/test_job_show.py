"""Owner ruling rul-k6nyv3r: one verb returns a job's verdict and whether
its branch has moved, so no supervisor hand-reads verdict.json, the
session log or the worktree. `foreman job show <job>` only reads."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from foreman import cli, fronts, paths, store
from foreman.caller import SESSION_ENV

V5_BRIEF = '''\
name = "shows"
goal = "A front whose supervisor reads a job's verdict by one verb."
finish-line = "job show prints the verdict and whether the branch moved."
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
work = "wshows"
target = "main"
check = "true"
land = "push"
'''

SID = "ses-wrk0001"
BUILT_BY_8_7A = pytest.mark.xfail(
    strict=True, reason="job-8.7a builds this; its worker removes this marker")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=test@example.invalid",
         "-c", "user.name=foreman-test", *args],
        check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def front(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "init")
    src = tmp_path / "src-repo"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q", str(src)], check=True)
    git(src, "commit", "-q", "--allow-empty", "-m", "init")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src), str(bare)],
                   check=True, capture_output=True)
    brief = tmp_path / "brief"
    brief.mkdir()
    (brief / "brief.toml").write_text(V5_BRIEF.format(url=bare),
                                      encoding="utf-8")
    assert fronts.front_add_main(str(brief)) == 0
    return tmp_path


def worktree_with_branch(root: Path, commits: int) -> tuple[Path, str]:
    repo = root / "worktree"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "wshows", "-q", str(repo)], check=True)
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "job/shows-1")
    for n in range(commits):
        git(repo, "commit", "-q", "--allow-empty", "-m", f"unit {n}")
    return repo, base_sha


def returned_job(repo: Path, base_sha: str) -> None:
    verdict = paths.session_verdict_path(SID)
    verdict.parent.mkdir(parents=True, exist_ok=True)
    verdict.write_text(json.dumps({
        "passed": True, "summary": "the door refuses an equal verify",
        "findings": []}), encoding="utf-8")
    store.append_ledger(paths.front_jobs_path("shows"), {
        "id": "job-1", "front": "shows", "task": "tsk-1", "kind": "implement",
        "role": "opus", "state": "returned", "exit_code": 0,
        "session": SID, "branch": "job/shows-1", "base": "wshows",
        "base_sha": base_sha, "worktree": str(repo),
        "verdict_path": str(verdict)})


def run(argv: list[str]) -> int:
    try:
        return cli.main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


@BUILT_BY_8_7A
def test_job_show_prints_the_verdict_and_a_moved_branch(front, capsys):
    repo, base_sha = worktree_with_branch(front, commits=2)
    returned_job(repo, base_sha)
    assert run(["job", "show", "job-1"]) == 0
    out = capsys.readouterr().out
    assert "returned" in out
    assert "the door refuses an equal verify" in out
    assert "job/shows-1" in out
    assert "moved: yes" in out and "2 commits" in out and base_sha[:7] in out


@BUILT_BY_8_7A
def test_job_show_says_when_the_branch_has_not_moved(front, capsys):
    repo, base_sha = worktree_with_branch(front, commits=0)
    returned_job(repo, base_sha)
    assert run(["job", "show", "job-1"]) == 0
    assert "moved: no" in capsys.readouterr().out


@BUILT_BY_8_7A
def test_job_show_refuses_an_unknown_job(front, capsys):
    assert run(["job", "show", "job-nosuch"]) != 0
    err = capsys.readouterr().err
    assert "refused" in err and "job-nosuch" in err
