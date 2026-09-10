"""Finish-line f4: status prints the queue, milestone pieces and the tree."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUSE = ("f4", "screen")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "status.py"
_NEEDLE = "    lines.extend(_v5_tree_lines(name))\n"
_PATCH = "    lines.extend([])\n"

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "status shows the queue, milestones and tree."
decisions = [
  "The screen is rebuilt to the new shape.",
]
supervisor = "fake:high"
team = [
  "fake:high:1:supervisor",
  "fake:high:1:builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "{work}"
target = "main"
check = "python -m pytest tests -q"
'''


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True)


def _field(out: str, prefix: str) -> str:
    needle = prefix if prefix.endswith(" ") else prefix + " "
    for line in out.splitlines():
        if line.startswith(needle):
            return line[len(needle):].strip()
    raise Failure(f"no {prefix!r} line in output:\n{out[-2000:]}")


def _drop_pyc(path: Path) -> None:
    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return
    stem = path.stem
    for item in cache.iterdir():
        if item.name.startswith(stem + "."):
            item.unlink(missing_ok=True)


def _bare(world) -> Path:
    stored = world.get("_f4_bare")
    if stored is not None:
        return Path(stored)
    root = Path(world["repo_a"]).parent / "f4-screen"
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
    world["_f4_bare"] = str(bare)
    return bare


def _ok(argv: list[str], session: str | None = None) -> str:
    proc = run_foreman(argv, session=session)
    if proc.returncode != 0:
        raise Failure(
            f"foreman {' '.join(argv[:4])} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[-800:]}")
    return proc.stdout


def _make_room() -> None:
    run_foreman(["cap", "fake", "99"])
    for name in ("alpha", "beta"):
        run_foreman(["front", "release", name])


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_f4_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_f4_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "status omits the tree block so the screen no longer draws the fold",
    apply, restore,
)


def run(world) -> str:
    assert_world()
    _make_room()
    n = world.get("_f4_n", 0) + 1
    world["_f4_n"] = n
    name = f"f4s{n}"
    work = f"w{name}"
    bare = _bare(world)
    directory = Path(world["specs"]) / f"f4-{name}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(name=name, url=str(bare), work=work), encoding="utf-8")
    _ok(["front", "add", str(directory)])
    sid = _field(_ok(["register", "--role", "supervisor", "--front", name,
                      "--pid", str(os.getpid())]), "session:")
    mil = _ok(
        ["milestone", "add", name, "--title", "The screen",
         "--done-when", "status shows the v5 shape",
         "--verify", "python -m pytest tests -q",
         "--reason", "decision 15",
         "--break", "drop the tree block",
         "--id", f"mil-f4{n}"],
        sid).strip()
    _ok(["node", "add", name, "--parent", name, "--kind", "milestone",
         "--title", "The screen",
         "--verify", "python -m pytest tests -q",
         "--must-not-touch", "the live state directory",
         "--reason", "the tree needs this node",
         "--break", "drop the tree block",
         "--repo", "foreman", "--id", mil], sid)
    tsk = f"tsk-f4{n}"
    _ok(["node", "add", name, "--parent", mil, "--kind", "task",
         "--title", "status shape",
         "--verify", "python -m pytest tests -q",
         "--must-not-touch", "the live state directory",
         "--reason", "the tree needs this node",
         "--break", "drop the tree block",
         "--repo", "foreman", "--id", tsk], sid)
    job = f"job-f4{n}"
    _ok(["node", "add", name, "--parent", tsk, "--kind", "job",
         "--title", "implement it",
         "--verify", "python -m pytest tests -q",
         "--must-not-touch", "the live state directory",
         "--reason", "the tree needs this node",
         "--break", "drop the tree block",
         "--repo", "foreman", "--id", job, "--role", "builder"], sid)
    _ok(["front", "reserve", name, "--phase", "builders"])
    status = run_foreman(["status"])
    if status.returncode != 0:
        raise Failure(f"status failed: {status.stderr[-800:]}")
    out = status.stdout
    if "queue:" not in out:
        raise Failure(f"status printed no queue block:\n{out[-1500:]}")
    if name not in out:
        raise Failure(f"status queue missed {name}:\n{out[-1500:]}")
    if "reserved" not in out:
        raise Failure(f"status printed no reservations:\n{out[-1500:]}")
    if "pieces" not in out or "split from" not in out:
        raise Failure(
            f"status missed milestone pieces/split count:\n{out[-1500:]}")
    if "tree:" not in out:
        raise Failure(f"status printed no tree:\n{out[-1500:]}")
    if mil not in out or tsk not in out or job not in out:
        raise Failure(
            f"status tree missed {mil}/{tsk}/{job}:\n{out[-1500:]}")
    return (
        f"status queue names {name} with reserved; "
        f"milestone lines carry pieces and split from; "
        f"tree draws {mil}, {tsk}, {job}"
    )
