"""Clause 3.2: supervisor queue verbs; a hand launch is refused."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUSE = ("3.2", "queue_verbs")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "progress.py"
_NEEDLE = (
    "                if kind != \"job\":\n"
    "                    violations.append(\n"
    "                        f\"{nid} is kind {kind}; only a job is queued\")\n"
)
_PATCH = (
    "                if False and kind != \"job\":\n"
    "                    violations.append(\n"
    "                        f\"{nid} is kind {kind}; only a job is queued\")\n"
)

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

_SPEC = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/front-v5 -q\n"
)


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
    stored = world.get("_32_bare")
    if stored is not None:
        return Path(stored)
    root = Path(world["repo_a"]).parent / "m3-qv"
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
    world["_32_bare"] = str(bare)
    return bare


def _add_front(world, name: str, url: str) -> None:
    directory = Path(world["specs"]) / f"m3-qv-{name}"
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


def _add_argv(front: str, parent: str, kind: str, title: str, **flags) -> list[str]:
    argv = [
        "node", "add", front, "--parent", parent, "--kind", kind,
        "--title", title,
        "--verify", "python -m pytest tests -q",
        "--must-not-touch", "the live state directory",
        "--reason", "the tree needs this node",
        "--break", "admit a job as a parent",
        "--repo", "foreman",
    ]
    if flags.get("role") is not None:
        argv.extend(["--role", flags["role"]])
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    for item in flags.get("after") or []:
        argv.extend(["--after", item])
    return argv


def _add_ok(front: str, sid: str, parent: str, kind: str, title: str,
            **flags) -> str:
    proc = run_foreman(
        _add_argv(front, parent, kind, title, **flags), session=sid)
    if proc.returncode != 0:
        raise Failure(
            f"node add {kind} {title!r} failed: {proc.stderr[-800:]}")
    nid = proc.stdout.strip()
    if not nid:
        raise Failure(f"node add printed {nid!r}")
    return nid


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_32_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_32_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "skip the kind check so job queue admits a task",
    apply, restore,
)


def run(world) -> str:
    assert_world()
    bare = _bare(world)
    n = world.get("_32_n", 0) + 1
    world["_32_n"] = n
    name = f"qv{n}"
    _add_front(world, name, str(bare))
    sid = _register(name)
    mil = _add_ok(name, sid, name, "milestone", "milestone one",
                  node_id=f"mil-qv{n}")
    tsk = _add_ok(name, sid, mil, "task", "the door",
                  node_id=f"tsk-qv{n}")
    first = _add_ok(name, sid, tsk, "job", "implement the door",
                    role="builder", node_id=f"job-qva{n}")
    second = _add_ok(name, sid, tsk, "job", "the next unit",
                     role="builder", node_id=f"job-qvb{n}",
                     after=[first])
    third = _add_ok(name, sid, tsk, "job", "another unit",
                    role="builder", node_id=f"job-qvc{n}")
    on_task = run_foreman(["job", "queue", name, tsk], session=sid)
    if on_task.returncode == 0:
        raise Failure("job queue on a task was admitted")
    err = on_task.stderr or ""
    if "kind" not in err or "task" not in err:
        raise Failure(f"task queue refusal did not name the kind: {err[-800:]}")
    queued = run_foreman(["job", "queue", name, second], session=sid)
    if queued.returncode != 0:
        raise Failure(f"job queue failed: {queued.stderr[-800:]}")
    if f"waits: dependency {first}" not in queued.stdout:
        raise Failure(
            f"queued job did not wait on dependency: {queued.stdout[-800:]}")
    extra = run_foreman(["job", "queue", name, third], session=sid)
    if extra.returncode != 0:
        raise Failure(f"job queue third failed: {extra.stderr[-800:]}")
    listed = run_foreman(["job", "list", name], session=sid)
    if listed.returncode != 0:
        raise Failure(f"job list failed: {listed.stderr[-800:]}")
    if second not in listed.stdout or third not in listed.stdout:
        raise Failure(f"job list missing queued jobs:\n{listed.stdout[-800:]}")
    fronted = run_foreman(["job", "front", name, second], session=sid)
    if fronted.returncode != 0:
        raise Failure(f"job front failed: {fronted.stderr[-800:]}")
    edited = run_foreman(
        ["job", "edit", name, second, "--what", "narrower unit"],
        session=sid)
    if edited.returncode != 0:
        raise Failure(f"job edit failed: {edited.stderr[-800:]}")
    cancelled = run_foreman(["job", "cancel", name, third], session=sid)
    if cancelled.returncode != 0:
        raise Failure(f"job cancel failed: {cancelled.stderr[-800:]}")
    after = run_foreman(["job", "list", name], session=sid)
    if after.returncode != 0:
        raise Failure(f"job list after edits failed: {after.stderr[-800:]}")
    lines = after.stdout.splitlines()
    if not lines or not lines[0].startswith(second):
        raise Failure(f"job front did not move {second} first:\n{after.stdout}")
    if third in after.stdout:
        raise Failure(f"cancelled job still listed:\n{after.stdout}")
    spec = Path(world["specs"]) / f"qv-hand-{n}.md"
    spec.write_text(_SPEC, encoding="utf-8")
    root = Path(world["repo_a"]).parent
    hand = run_foreman([
        "launch", "grok", "grok", str(spec),
        "--repo", str(world["repo_a"]),
        "--worktree", str(root / f"wt-qv-{n}"),
        "--dry-run",
    ], session=sid)
    if hand.returncode == 0:
        raise Failure("supervisor hand launch --dry-run was admitted")
    err = hand.stderr or ""
    if "job queue" not in err:
        raise Failure(
            f"hand launch refusal did not name job queue: {err[-800:]}")
    return (
        f"task named kind; {second} waits: dependency {first}; "
        f"front/edit/cancel changed job list; hand launch named job queue"
    )
