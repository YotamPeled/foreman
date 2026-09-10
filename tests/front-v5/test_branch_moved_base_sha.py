"""A failed job's branch counts as moved against the sha it was cut from.

Landing a failed job's branch by fast-forward makes the base branch equal
to it, so comparing with the base branch's current head judges the work
"never moved" exactly when it has landed. The job records ``base_sha``;
the comparison uses it where it is recorded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foreman import progress


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=test@example.invalid",
         "-c", "user.name=foreman-test", *args],
        check=True, capture_output=True, text=True).stdout.strip()


def repo_at_base(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "front", "-q", str(repo)], check=True)
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def record(repo: Path, branch: str, base_sha: str | None) -> dict:
    line = {"id": "job-1", "state": "failed", "branch": branch,
            "base": "front", "worktree": str(repo)}
    if base_sha is not None:
        line["base_sha"] = base_sha
    return line


def land_by_fast_forward(repo: Path, branch: str) -> None:
    git(repo, "checkout", "-q", "-b", branch)
    git(repo, "commit", "-q", "--allow-empty", "-m", "the fix")
    git(repo, "checkout", "-q", "front")
    git(repo, "merge", "-q", "--ff-only", branch)


@pytest.mark.xfail(strict=True, reason="job-7.3a builds this; its worker removes this marker")
def test_landed_branch_counts_as_moved_past_its_recorded_base_sha(tmp_path):
    repo, base_sha = repo_at_base(tmp_path)
    land_by_fast_forward(repo, "job/fix")
    assert git(repo, "rev-parse", "front") == git(repo, "rev-parse", "job/fix")
    assert progress.job_branch_moved(record(repo, "job/fix", base_sha)) is True


def test_branch_that_never_moved_past_its_base_sha_does_not_count(tmp_path):
    repo, base_sha = repo_at_base(tmp_path)
    git(repo, "branch", "job/idle")
    assert progress.job_branch_moved(record(repo, "job/idle", base_sha)) is False


def test_record_without_base_sha_keeps_the_base_branch_comparison(tmp_path):
    repo, _ = repo_at_base(tmp_path)
    land_by_fast_forward(repo, "job/fix")
    assert progress.job_branch_moved(record(repo, "job/fix", None)) is False
