"""launch --base resolves against origin and prints the sha.

A clone whose local main sits one commit behind a bare origin is the
fixture; without the fetch the worktree would land on the stale local
head and the job line would carry no base_sha.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from foreman import cli, paths, store
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter


SPEC_OK = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v0 -q\n"
)


class FakeAdapter(PoolAdapter):
    """Fake pool: writes the pid file and returns, spawning nothing real."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    def launch(self, ctx: LaunchContext) -> int:
        pid = os.getpid()
        ctx.pid_path.write_text(f"{pid}\n", encoding="utf-8")
        return pid

    def observe(self, session: Session) -> dict:
        return {"transcript_mtime": None, "cpu_s": 0.0, "finish_present": False}

    def command_str(self, ctx: LaunchContext) -> str:
        return f"fake-exec --worktree {ctx.worktree}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    return tmp_path


@pytest.fixture()
def fake_pool():
    from foreman import pools

    pools.register("fake", FakeAdapter())
    try:
        yield FakeAdapter()
    finally:
        pools.unregister("fake")


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def make_repo_with_origin(root: Path) -> tuple[Path, Path]:
    """A repo with a bare origin beside it, both under tmp_path.

    Copied from tests/v2/test_merge_desk.py; not imported across test dirs.
    """
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-qm", "seed")
    origin = root / "origin.git"
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    return repo, origin


def clone_behind_origin(root: Path) -> tuple[Path, Path, str, str]:
    """Clone whose local main is one commit behind origin.

    Returns (clone, origin, local_sha, origin_sha).
    """
    repo, origin = make_repo_with_origin(root)
    (repo / "on-origin.txt").write_text("origin only\n", encoding="utf-8")
    git(repo, "add", "on-origin.txt")
    git(repo, "commit", "-qm", "on origin")
    origin_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    local_sha = git(repo, "rev-parse", "HEAD")
    assert local_sha != origin_sha
    return repo, origin, local_sha, origin_sha


def write_spec(root: Path) -> str:
    spec = root / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    return str(spec)


def launch(argv: list[str]) -> int:
    return cli.main(["launch", *argv])


def behind_line(branch: str, local_sha: str, origin_sha: str) -> str:
    return (f"base {branch}: local {local_sha[:7]} is behind "
            f"origin {origin_sha[:7]}; using origin")


def test_base_main_cuts_from_origin_when_local_is_behind(
        env, fake_pool, capsys):
    repo, _origin, local_sha, origin_sha = clone_behind_origin(env / "world")
    spec = write_spec(env)
    worktree = env / "wt-base"
    rc = launch(["muse", "fake", spec, "--repo", str(repo),
                 "--worktree", str(worktree), "--base", "main",
                 "--branch", "job/from-origin", "--front", "corpus",
                 "--job", "job-base1", "--units", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert behind_line("main", local_sha, origin_sha) in out.splitlines()
    assert git(worktree, "rev-parse", "HEAD~0") == origin_sha
    jobs = store.read_ledger(paths.front_jobs_path("corpus"))
    assert jobs[-1]["id"] == "job-base1"
    assert jobs[-1]["base"] == "main"
    assert jobs[-1]["base_sha"] == origin_sha


def test_dry_run_prints_behind_line_and_writes_nothing(
        env, fake_pool, capsys):
    repo, _origin, local_sha, origin_sha = clone_behind_origin(env / "world")
    spec = write_spec(env)
    worktree = env / "wt-dry"
    rc = launch(["muse", "fake", spec, "--repo", str(repo),
                 "--worktree", str(worktree), "--base", "main",
                 "--branch", "job/from-origin", "--front", "corpus",
                 "--job", "job-dry", "--units", "0", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert behind_line("main", local_sha, origin_sha) in out.splitlines()
    assert not worktree.exists()
    assert git(repo, "branch", "--list", "job/from-origin") == ""
    assert not paths.front_jobs_path("corpus").exists()
    assert not paths.roster_path().exists()


def test_no_remote_cuts_from_local_and_prints_the_line(
        env, fake_pool, capsys):
    repo, _origin, local_sha, _origin_sha = clone_behind_origin(env / "world")
    git(repo, "remote", "remove", "origin")
    spec = write_spec(env)
    worktree = env / "wt-noremote"
    rc = launch(["muse", "fake", spec, "--repo", str(repo),
                 "--worktree", str(worktree), "--base", "main",
                 "--branch", "job/local-main", "--front", "corpus",
                 "--job", "job-local", "--units", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert (f"base main: no remote; local {local_sha[:7]}"
            in out.splitlines())
    assert git(worktree, "rev-parse", "HEAD~0") == local_sha
    jobs = store.read_ledger(paths.front_jobs_path("corpus"))
    assert jobs[-1]["base"] == "main"
    assert jobs[-1]["base_sha"] == local_sha


def test_base_naming_a_branch_origin_lacks_is_refused(
        env, fake_pool, capsys):
    repo, _origin, _local_sha, _origin_sha = clone_behind_origin(env / "world")
    spec = write_spec(env)
    worktree = env / "wt-nope"
    rc = launch(["muse", "fake", spec, "--repo", str(repo),
                 "--worktree", str(worktree), "--base", "nope",
                 "--branch", "job/nope"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "nope" in err
    assert not worktree.exists()
    assert git(repo, "branch", "--list", "job/nope") == ""


def test_absent_base_still_cuts_from_the_checkout(
        env, fake_pool, capsys):
    """``default_base`` stays the checkout's own branch when ``--base``
    is absent, even if origin is ahead."""
    repo, _origin, local_sha, origin_sha = clone_behind_origin(env / "world")
    spec = write_spec(env)
    worktree = env / "wt-default"
    rc = launch(["muse", "fake", spec, "--repo", str(repo),
                 "--worktree", str(worktree),
                 "--branch", "job/from-local"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "using origin" not in out
    assert git(worktree, "rev-parse", "HEAD~0") == local_sha
    assert git(worktree, "rev-parse", "HEAD~0") != origin_sha


def test_ahead_of_origin_still_cuts_from_origin(env, fake_pool, capsys):
    repo, _origin = make_repo_with_origin(env / "world")
    origin_sha = git(repo, "rev-parse", "HEAD")
    (repo / "local-only.txt").write_text("local only\n", encoding="utf-8")
    git(repo, "add", "local-only.txt")
    git(repo, "commit", "-qm", "local only")
    local_sha = git(repo, "rev-parse", "HEAD")
    spec = write_spec(env)
    worktree = env / "wt-ahead"
    rc = launch(["muse", "fake", spec, "--repo", str(repo),
                 "--worktree", str(worktree), "--base", "main",
                 "--branch", "job/from-origin"])
    assert rc == 0
    out = capsys.readouterr().out
    assert (f"base main: local {local_sha[:7]} is ahead of "
            f"origin {origin_sha[:7]}; using origin") in out.splitlines()
    assert git(worktree, "rev-parse", "HEAD~0") == origin_sha


def test_diverged_from_origin_still_cuts_from_origin(
        env, fake_pool, capsys):
    repo, _origin, local_sha, origin_sha = clone_behind_origin(env / "world")
    (repo / "local-only.txt").write_text("local only\n", encoding="utf-8")
    git(repo, "add", "local-only.txt")
    git(repo, "commit", "-qm", "local only")
    local_sha = git(repo, "rev-parse", "HEAD")
    spec = write_spec(env)
    worktree = env / "wt-diverged"
    rc = launch(["muse", "fake", spec, "--repo", str(repo),
                 "--worktree", str(worktree), "--base", "main",
                 "--branch", "job/from-origin"])
    assert rc == 0
    out = capsys.readouterr().out
    assert (f"base main: local {local_sha[:7]} is diverged from "
            f"origin {origin_sha[:7]}; using origin") in out.splitlines()
    assert git(worktree, "rev-parse", "HEAD~0") == origin_sha
