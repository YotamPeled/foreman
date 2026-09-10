"""Clause 2.2: node add door, list indented by depth, revise needs a reason."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUSE = ("2.2", "node_door")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "node.py"
_NEEDLE = "                if parent_kind in CHILDLESS_NODE_KINDS:\n"
_PATCH = "                if False and parent_kind in CHILDLESS_NODE_KINDS:\n"

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
    stored = world.get("_22_bare")
    if stored is not None:
        return Path(stored)
    root = Path(world["repo_a"]).parent / "m2-node"
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
    world["_22_bare"] = str(bare)
    return bare


def _add_front(world, name: str, url: str) -> None:
    directory = Path(world["specs"]) / f"m2-nd-{name}"
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
    n = world.get("_22_n", 0) + 1
    world["_22_n"] = n
    name = f"nd{n}"
    _add_front(world, name, str(bare))
    return name, _register(name)


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_22_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_22_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "skip the CHILDLESS_NODE_KINDS parent check in node_add_main",
    apply, restore,
)


def _add_argv(front: str, parent: str, kind: str, title: str, *,
              include_break: bool = True, source: str | None = None
              ) -> list[str]:
    argv = [
        "node", "add", front, "--parent", parent, "--kind", kind,
        "--title", title,
        "--verify", "python -m pytest tests -q",
        "--must-not-touch", "the live state directory",
        "--reason", "the tree needs this node",
        "--repo", "foreman",
    ]
    if include_break:
        argv.extend(["--break", "admit a job as a parent"])
    if source is not None:
        argv.extend(["--source", source])
    return argv


def _add_ok(front: str, sid: str, parent: str, kind: str, title: str) -> str:
    proc = run_foreman(
        _add_argv(front, parent, kind, title), session=sid)
    if proc.returncode != 0:
        raise Failure(
            f"node add {kind} {title!r} failed: {proc.stderr[-800:]}")
    nid = proc.stdout.strip()
    if not nid.startswith("nod-"):
        raise Failure(f"node add printed {nid!r}, not a nod- id")
    return nid


def run(world) -> str:
    assert_world()
    name, sid = _front(world)
    mil = _add_ok(name, sid, name, "milestone", "milestone one")
    tsk = _add_ok(name, sid, mil, "task", "the door")
    job = _add_ok(name, sid, tsk, "job", "implement the door")
    under_job = run_foreman(
        _add_argv(name, job, "task", "under a job"), session=sid)
    if under_job.returncode == 0:
        raise Failure("a node under a job was admitted")
    err = under_job.stderr or ""
    if "job" not in err:
        raise Failure(f"job-parent refusal did not name job: {err[-800:]}")
    no_break = run_foreman(
        _add_argv(name, name, "milestone", "no break", include_break=False),
        session=sid)
    if no_break.returncode == 0:
        raise Failure("node add without --break was admitted")
    err = no_break.stderr or ""
    if "break" not in err:
        raise Failure(f"missing --break refusal did not name the field: {err[-800:]}")
    derived = run_foreman(
        _add_argv(name, mil, "derived", "a copy"), session=sid)
    if derived.returncode == 0:
        raise Failure("derived node without --source was admitted")
    err = derived.stderr or ""
    if "source" not in err:
        raise Failure(f"derived refusal did not name source: {err[-800:]}")
    listed = run_foreman(["node", "list", name], session=sid)
    if listed.returncode != 0:
        raise Failure(f"node list failed: {listed.stderr[-800:]}")
    want = [
        f"{mil}  milestone  milestone one",
        f"  {tsk}  task  the door  0/1",
        f"    {job}  job  implement the door",
    ]
    got = listed.stdout.splitlines()
    if got != want:
        raise Failure(f"node list not indented by depth:\n{listed.stdout[-800:]}")
    revise = run_foreman(
        ["node", "revise", name, mil, "--title", "renamed"], session=sid)
    if revise.returncode == 0:
        raise Failure("node revise without --reason was admitted")
    err = revise.stderr or ""
    if "reason" not in err:
        raise Failure(f"revise refusal did not name reason: {err[-800:]}")
    return (
        f"chain {mil}/{tsk}/{job}; under job named job; "
        f"no --break named break; derived named source; "
        f"list indented; revise named reason"
    )
