"""Clause 1.3: launch --base prints the behind line with origin's sha."""

from __future__ import annotations

import subprocess
from pathlib import Path

CLAUSE = ("1.3", "base remote")

_SPEC = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v0 -q\n"
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(
            f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout)[-400:]}")
    return proc.stdout.strip()


def _behind_world(world) -> tuple[Path, str, str]:
    stored = world.get("_13_clone")
    if stored is not None:
        return Path(stored), world["_13_local"], world["_13_origin"]
    root = Path(world["repo_a"]).parent / "m1-behind"
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "proof@example.invalid")
    _git(repo, "config", "user.name", "proof")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-qm", "seed")
    origin = root / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        capture_output=True, text=True, check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    (repo / "on-origin.txt").write_text("origin only\n", encoding="utf-8")
    _git(repo, "add", "on-origin.txt")
    _git(repo, "commit", "-qm", "on origin")
    origin_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    local_sha = _git(repo, "rev-parse", "HEAD")
    if local_sha == origin_sha:
        raise Failure("clone is not behind origin")
    world["_13_clone"] = str(repo)
    world["_13_local"] = local_sha
    world["_13_origin"] = origin_sha
    world["_13_root"] = str(root)
    return repo, local_sha, origin_sha


def apply(world) -> None:
    repo, _local, origin_sha = _behind_world(world)
    _git(repo, "reset", "-q", "--hard", origin_sha)


def restore(world) -> None:
    repo = world.get("_13_clone")
    local = world.get("_13_local")
    if repo and local:
        _git(Path(repo), "reset", "-q", "--hard", local)


BREAK = ("fast-forward the clone onto origin so it is not behind",
         apply, restore)


def run(world) -> str:
    assert_world()
    repo, local_sha, origin_sha = _behind_world(world)
    spec = Path(world["specs"]) / "base-remote.md"
    spec.write_text(_SPEC, encoding="utf-8")
    n = world.get("_13_n", 0) + 1
    world["_13_n"] = n
    worktree = Path(world["_13_root"]) / f"wt-{n}"
    proc = run_foreman([
        "launch", "muse", "fake", str(spec),
        "--repo", str(repo),
        "--worktree", str(worktree),
        "--base", "main",
        "--branch", f"job/from-origin-{n}",
        "--units", "0",
        "--effort", "xhigh",
        "--dry-run",
    ])
    if proc.returncode != 0:
        raise Failure(
            f"launch --dry-run --base main exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[-800:]}")
    line = (f"base main: local {local_sha[:7]} is behind "
            f"origin {origin_sha[:7]}; using origin")
    if line not in proc.stdout.splitlines():
        raise Failure(
            f"missing behind line {line!r} in:\n{proc.stdout[-1500:]}")
    return f"behind line names origin {origin_sha[:7]}"
