"""Clause 2.5: a derived node with a source is admitted; it takes no children."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUSE = ("2.5", "derived_node")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "entities.py"
_NEEDLE = 'CHILDLESS_NODE_KINDS = ("job", "derived")\n'
_PATCH = 'CHILDLESS_NODE_KINDS = ("job",)\n'

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
]
supervisor = "grok-4.6:high"
team = [
  "grok-4.6:high:1:supervisor",
  "grok-4.6:high:1:builder",
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
    stored = world.get("_25_bare")
    if stored is not None:
        return Path(stored)
    root = Path(world["repo_a"]).parent / "m2-derived"
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
    world["_25_bare"] = str(bare)
    return bare


def _add_front(world, name: str, url: str) -> None:
    directory = Path(world["specs"]) / f"m2-der-{name}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(name=name, url=url), encoding="utf-8")
    added = run_foreman(["front", "add", str(directory)])
    if added.returncode != 0:
        raise Failure(f"front add {name} failed: {added.stderr[-800:]}")


def _register(front: str) -> str:
    proc = run_foreman(
        ["register", "--role", "supervisor", "--front", front,
         "--pid", str(os.getpid())])
    if proc.returncode != 0:
        raise Failure(f"register supervisor failed: {proc.stderr[-800:]}")
    return _field(proc.stdout, "session:")


def _front(world) -> tuple[str, str]:
    bare = _bare(world)
    n = world.get("_25_n", 0) + 1
    world["_25_n"] = n
    name = f"dn{n}"
    _add_front(world, name, str(bare))
    return name, _register(name)


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_25_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_25_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "drop derived from CHILDLESS_NODE_KINDS",
    apply, restore,
)


def _add_argv(front: str, parent: str, kind: str, title: str, *,
              source: str | None = None) -> list[str]:
    argv = [
        "node", "add", front, "--parent", parent, "--kind", kind,
        "--title", title,
        "--verify", "python -m pytest tests -q",
        "--must-not-touch", "the live state directory",
        "--reason", "the tree needs this node",
        "--break", "admit a child of a derived node",
        "--repo", "foreman",
    ]
    if source is not None:
        argv.extend(["--source", source])
    return argv


def _add_ok(front: str, sid: str, parent: str, kind: str, title: str, *,
            source: str | None = None) -> str:
    proc = run_foreman(
        _add_argv(front, parent, kind, title, source=source), session=sid)
    if proc.returncode != 0:
        raise Failure(
            f"node add {kind} {title!r} failed: {proc.stderr[-800:]}")
    return proc.stdout.strip()


def run(world) -> str:
    assert_world()
    name, sid = _front(world)
    mil = _add_ok(name, sid, name, "milestone", "milestone one")
    derived = _add_ok(
        name, sid, mil, "derived", "a copy",
        source="fronts/v5/tree.jsonl")
    listed = run_foreman(["node", "list", name], session=sid)
    if listed.returncode != 0:
        raise Failure(f"node list failed: {listed.stderr[-800:]}")
    want = [
        f"{mil}  milestone  milestone one",
        f"  {derived}  derived  a copy",
    ]
    got = listed.stdout.splitlines()
    if got != want:
        raise Failure(
            f"node list did not show derived under its parent:\n"
            f"{listed.stdout[-800:]}")
    child = run_foreman(
        _add_argv(name, derived, "task", "under derived"), session=sid)
    if child.returncode == 0:
        raise Failure("a child under a derived node was admitted")
    err = child.stderr or ""
    if "derived" not in err:
        raise Failure(
            f"child-of-derived refusal did not name derived: {err[-800:]}")
    return (
        f"derived {derived} under {mil}; child under it named derived"
    )
