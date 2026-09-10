"""Finish-line f2: supervisor queues two leaves; one tick starts the first."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUSE = ("f2", "queued and started")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "collector.py"
_NEEDLE = (
    "            if (not started and waits in (\"\", \"ready\")\n"
    "                    and role != \"script\"):\n"
)
_PATCH = (
    "            if False and (not started and waits in (\"\", \"ready\")\n"
    "                    and role != \"script\"):\n"
)

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "Queued leaves are started by the runtime."
decisions = [
  "Queues are 100% mechanical.",
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


def _bare(world) -> tuple[Path, Path]:
    stored = world.get("_f2_bare")
    if stored is not None:
        return Path(stored), Path(world["_f2_src"])
    root = Path(world["repo_a"]).parent / "f2-queue"
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
    _git(bare, "config", "user.email", "proof@example.invalid")
    _git(bare, "config", "user.name", "proof")
    world["_f2_bare"] = str(bare)
    world["_f2_src"] = str(src)
    return bare, src


def _add_front(world, name: str, url: str, work: str) -> None:
    directory = Path(world["specs"]) / f"f2-{name}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(name=name, url=url, work=work), encoding="utf-8")
    added = run_foreman(["front", "add", str(directory)])
    if added.returncode != 0:
        raise Failure(f"front add {name} failed: {added.stderr[-800:]}")


def _ensure_work(src: Path, bare: Path, work: str) -> None:
    proc = _git(src, "branch", work, "main")
    if proc.returncode != 0 and "already exists" not in (proc.stderr or ""):
        raise Failure(f"git branch {work} failed: {proc.stderr[-400:]}")
    proc = subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", "-q", "origin",
         f"+refs/heads/{work}:refs/remotes/origin/{work}"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"fetch origin {work} failed: {proc.stderr[-400:]}")


def _register(front: str) -> str:
    proc = run_foreman(
        ["register", "--role", "supervisor", "--front", front,
         "--pid", str(os.getpid())])
    if proc.returncode != 0:
        raise Failure(f"register supervisor failed: {proc.stderr[-800:]}")
    return _field(proc.stdout, "session:")


def _add_ok(front: str, sid: str, parent: str, kind: str, title: str,
            **flags) -> str:
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
    if flags.get("what") is not None:
        argv.extend(["--what", flags["what"]])
    proc = run_foreman(argv, session=sid)
    if proc.returncode != 0:
        raise Failure(
            f"node add {kind} {title!r} failed: {proc.stderr[-800:]}")
    return proc.stdout.strip()


def _make_room() -> None:
    run_foreman(["cap", "fake", "99"])
    for name in ("alpha", "beta"):
        run_foreman(["front", "release", name])


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_f2_collector"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_f2_collector", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "the queue tick never calls start_queued so no leaf becomes running",
    apply, restore,
)


def run(world) -> str:
    assert_world()
    _make_room()
    bare, src = _bare(world)
    n = world.get("_f2_n", 0) + 1
    world["_f2_n"] = n
    name = f"f2q{n}"
    work = f"w{name}"
    _add_front(world, name, str(bare), work)
    _ensure_work(src, bare, work)
    sid = _register(name)
    mil = _add_ok(name, sid, name, "milestone", "milestone one",
                  node_id=f"mil-f2{n}")
    tsk = _add_ok(name, sid, mil, "task", "two leaves",
                  node_id=f"tsk-f2{n}")
    first = _add_ok(name, sid, tsk, "job", "the first leaf",
                    role="builder", node_id=f"job-f2a{n}",
                    what="Start this leaf first.")
    second = _add_ok(name, sid, tsk, "job", "the second leaf",
                     role="builder", node_id=f"job-f2b{n}",
                     what="Wait for a slot.")
    q1 = run_foreman(["job", "queue", name, first], session=sid)
    if q1.returncode != 0:
        raise Failure(f"queue first leaf failed: {q1.stderr[-800:]}")
    q2 = run_foreman(["job", "queue", name, second], session=sid)
    if q2.returncode != 0:
        raise Failure(f"queue second leaf failed: {q2.stderr[-800:]}")
    tick = run_foreman(["collector", "once"])
    if tick.returncode != 0:
        raise Failure(f"collector once failed: {tick.stderr[-800:]}")
    listed = run_foreman(["job", "list", name], session=sid)
    if listed.returncode != 0:
        raise Failure(f"job list failed: {listed.stderr[-800:]}")
    out = listed.stdout
    if first not in out or "running" not in out:
        raise Failure(f"job list did not show {first} running:\n{out[-800:]}")
    if f"{first}" in out:
        first_line = next((ln for ln in out.splitlines() if first in ln), "")
        if "running" not in first_line:
            raise Failure(
                f"first leaf line is not running: {first_line!r}\n{out[-800:]}")
    if second not in out:
        raise Failure(f"job list missing second leaf {second}:\n{out[-800:]}")
    second_line = next((ln for ln in out.splitlines() if second in ln), "")
    if "running" in second_line:
        raise Failure(f"second leaf was started too: {second_line!r}")
    if "waits:" not in second_line and "no slot" not in second_line:
        raise Failure(
            f"second leaf has no wait reason: {second_line!r}\n{out[-800:]}")
    return (
        f"tick started {first} running; {second} waits "
        f"{second_line.split('waits:')[-1].strip() if 'waits:' in second_line else 'no slot'}"
    )
