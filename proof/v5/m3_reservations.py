"""Clause 3.1: a front reserves builders against the fake pool cap."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUSE = ("3.1", "reservations")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "fronts.py"
_NEEDLE = "            if cap - reserved < count:\n"
_PATCH = "            if False and cap - reserved < count:\n"

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
]
supervisor = "fake:high"
team = [
  "fake:high:1:supervisor",
  "fake:high:2:builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5"
target = "main"
check = "python -m pytest tests -q"
'''


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True)


def _drop_pyc(path: Path) -> None:
    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return
    stem = path.stem
    for item in cache.iterdir():
        if item.name.startswith(stem + "."):
            item.unlink(missing_ok=True)


def _bare(world) -> Path:
    stored = world.get("_31_bare")
    if stored is not None:
        return Path(stored)
    root = Path(world["repo_a"]).parent / "m3-rsv"
    src = root / "src"
    src.mkdir(parents=True)
    for argv in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "proof@example.invalid"),
                 ("config", "user.name", "proof"),
                 ("commit", "-q", "--allow-empty", "-m", "init")):
        proc = _git(src, *argv)
        if proc.returncode != 0:
            raise Failure(f"git {' '.join(argv)} failed: {proc.stderr[-400:]}")
    bare = root / "remote.git"
    proc = subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(bare)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"bare clone failed: {proc.stderr[-400:]}")
    world["_31_bare"] = str(bare)
    return bare


def _add_front(world, name: str, url: str) -> None:
    directory = Path(world["specs"]) / f"m3-rsv-{name}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(name=name, url=url), encoding="utf-8")
    added = run_foreman(["front", "add", str(directory)])
    if added.returncode != 0:
        raise Failure(f"front add {name} failed: {added.stderr[-800:]}")


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_31_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_31_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "skip the pool cap check so a second front is admitted past reserved 2",
    apply, restore,
)


def run(world) -> str:
    assert_world()
    capped = run_foreman(["cap", "fake", "3"])
    if capped.returncode != 0:
        raise Failure(f"cap fake 3 failed: {capped.stderr[-800:]}")
    bare = _bare(world)
    n = world.get("_31_n", 0) + 1
    world["_31_n"] = n
    front_a = f"A{n}"
    front_b = f"B{n}"
    _add_front(world, front_a, str(bare))
    _add_front(world, front_b, str(bare))
    reserved = run_foreman(["front", "reserve", front_a, "--phase", "builders"])
    if reserved.returncode != 0:
        raise Failure(f"front reserve {front_a} failed: {reserved.stderr[-800:]}")
    refused = run_foreman(["front", "reserve", front_b, "--phase", "builders"])
    if refused.returncode == 0:
        raise Failure(f"front reserve {front_b} was admitted")
    err = refused.stderr or ""
    if "cap 3, reserved 2" not in err:
        raise Failure(
            f"B refusal did not name cap 3, reserved 2: {err[-800:]}")
    status = run_foreman(["status"])
    if status.returncode != 0:
        raise Failure(f"status failed: {status.stderr[-400:]}")
    if "held 0 / reserved 2 / cap 3" not in status.stdout:
        raise Failure(
            f"status missing held 0 / reserved 2 / cap 3:\n"
            f"{status.stdout[-1500:]}")
    released = run_foreman(["front", "release", front_a])
    if released.returncode != 0:
        raise Failure(f"front release {front_a} failed: {released.stderr[-800:]}")
    second = run_foreman(["front", "reserve", front_b, "--phase", "builders"])
    if second.returncode != 0:
        raise Failure(
            f"front reserve {front_b} after release failed: "
            f"{second.stderr[-800:]}")
    run_foreman(["front", "release", front_b])
    return (
        f"{front_a} reserved 2; {front_b} refused cap 3, reserved 2; "
        f"status held 0 / reserved 2 / cap 3; release let {front_b} reserve"
    )
