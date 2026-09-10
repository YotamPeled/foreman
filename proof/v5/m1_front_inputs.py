"""Clause 1.1: front add admits a v5-shape brief and refuses leftover tasks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

CLAUSE = ("1.1", "front inputs")

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
  "Landing is a queued script.",
]
supervisor = "grok-4.6:high"
team = [
  "grok-4.6:high:1:supervisor",
  "grok-4.6:high:2:builder",
  "opus-5:high:1:backup-builder",
  "astra-6:low:1:reviewer",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5"
target = "main"
check = "python -m pytest tests -q"
land = "push"
trailers = ["Signed-off-by: Foreman"]
pr-body = "the front"
'''

_TASK = '''
[[task]]
title = "a leftover task"
scope = """
WHAT: leftover.
INPUTS: none.
OUTPUTS: none.
OUT OF SCOPE: everything else.
"""
verify = "true"
size = 1
'''


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True)


def _bare(world) -> tuple[Path, str]:
    stored = world.get("_11_bare")
    if stored is not None:
        return Path(stored), world["_11_sha"]
    root = Path(world["repo_a"]).parent / "m1-front"
    src = root / "src"
    src.mkdir(parents=True)
    for argv in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "proof@example.invalid"),
                 ("config", "user.name", "proof"),
                 ("commit", "-q", "--allow-empty", "-m", "init")):
        proc = _git(src, *argv)
        if proc.returncode != 0:
            raise Failure(f"git {' '.join(argv)} failed: {proc.stderr[-400:]}")
    sha = _git(src, "rev-parse", "HEAD").stdout.strip()
    bare = root / "remote.git"
    proc = subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(bare)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"bare clone failed: {proc.stderr[-400:]}")
    world["_11_bare"] = str(bare)
    world["_11_sha"] = sha
    return bare, sha


def _write_brief(world, name: str, url: str, extra: str = "") -> Path:
    n = world.get("_11_n", 0) + 1
    world["_11_n"] = n
    directory = Path(world["specs"]) / f"v5brief-{n}-{name}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(name=name, url=url) + extra, encoding="utf-8")
    return directory


def apply(world) -> None:
    bare, _sha = _bare(world)
    proc = _git(bare, "branch", "v5")
    if proc.returncode != 0:
        raise Failure(f"BREAK apply could not create v5: {proc.stderr[-400:]}")


def restore(world) -> None:
    bare = world.get("_11_bare")
    if not bare:
        return
    _git(Path(bare), "branch", "-D", "v5")


BREAK = ("create the work branch on the remote so front add is refused",
         apply, restore)


def run(world) -> str:
    assert_world()
    bare, sha = _bare(world)
    n = world.get("_11_n", 0) + 1
    name = f"in{n}"
    added = run_foreman(["front", "add", str(_write_brief(world, name, str(bare)))])
    if added.returncode != 0:
        raise Failure(
            f"front add of a v5-shape brief exited {added.returncode}: "
            f"{(added.stderr or added.stdout)[-800:]}")
    shown = run_foreman(["front", "show", name, "--json"])
    if shown.returncode != 0:
        raise Failure(f"front show --json failed: {shown.stderr[-400:]}")
    record = json.loads(shown.stdout)
    if record.get("shape") != "v5":
        raise Failure(f"shape is {record.get('shape')!r}, not 'v5'")
    allocation = record.get("allocation")
    if allocation != {"grok": 2, "opus": 1, "astra": 1}:
        raise Failure(f"allocation is {allocation!r}")
    repos = record.get("repositories") or []
    if not repos or repos[0].get("base_sha") != sha:
        raise Failure(
            f"base_sha is {(repos[0].get('base_sha') if repos else None)!r}, "
            f"not the bare repo's {sha}")
    leftover = f"in{n}t"
    refused = run_foreman(
        ["front", "add", str(_write_brief(world, leftover, str(bare), _TASK))])
    if refused.returncode == 0:
        raise Failure("a v5 brief with [[task]] was admitted")
    err = refused.stderr or ""
    if "task" not in err:
        raise Failure(f"refusal did not name task: {err[-800:]}")
    return (
        f"front {name} shape=v5 allocation={allocation} "
        f"base_sha={sha[:7]}; [[task]] refused naming task"
    )
