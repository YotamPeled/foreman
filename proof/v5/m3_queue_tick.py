"""Clause 3.3: one collector tick starts a ready job headless."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUSE = ("3.3", "queue_tick")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "launch.py"
_NEEDLE = "        \"--headless\",\n"
_PATCH = "        \"--no-headless\",\n"

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
    stored = world.get("_33_bare")
    if stored is not None:
        return Path(stored), Path(world["_33_src"])
    root = Path(world["repo_a"]).parent / "m3-qt"
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
    world["_33_bare"] = str(bare)
    world["_33_src"] = str(src)
    return bare, src


def _add_front(world, name: str, url: str, work: str) -> None:
    directory = Path(world["specs"]) / f"m3-qt-{name}"
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
    proc = _git(bare, "branch", work, "main")
    if proc.returncode != 0 and "already exists" not in (proc.stderr or ""):
        raise Failure(f"bare branch {work} failed: {proc.stderr[-400:]}")


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
    for item in flags.get("after") or []:
        argv.extend(["--after", item])
    proc = run_foreman(argv, session=sid)
    if proc.returncode != 0:
        raise Failure(
            f"node add {kind} {title!r} failed: {proc.stderr[-800:]}")
    return proc.stdout.strip()


def _clear_launch_log(world) -> Path:
    path = Path(world["launch_log"])
    path.write_text("", encoding="utf-8")
    return path


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_33_launch"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_33_launch", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "start_queued passes --no-headless instead of --headless",
    apply, restore,
)


def run(world) -> str:
    assert_world()
    bare, src = _bare(world)
    n = world.get("_33_n", 0) + 1
    world["_33_n"] = n
    name = f"qt{n}"
    work = f"w{name}"
    _add_front(world, name, str(bare), work)
    _ensure_work(src, bare, work)
    sid = _register(name)
    mil = _add_ok(name, sid, name, "milestone", "milestone one",
                  node_id=f"mil-qt{n}")
    tsk = _add_ok(name, sid, mil, "task", "the door",
                  node_id=f"tsk-qt{n}")
    first = _add_ok(name, sid, tsk, "job", "the predecessor",
                    role="builder", node_id=f"job-qta{n}")
    landed = run_foreman(
        ["node", "revise", name, first, "--state", "landed",
         "--reason", "predecessor landed"],
        session=sid)
    if landed.returncode != 0:
        raise Failure(f"land predecessor failed: {landed.stderr[-800:]}")
    ready = _add_ok(name, sid, tsk, "job", "the ready unit",
                    role="builder", node_id=f"job-qtb{n}",
                    after=[first], what="Cut the oldest ready job.")
    waiting = _add_ok(name, sid, tsk, "job", "the next unit",
                      role="builder", node_id=f"job-qtc{n}")
    q1 = run_foreman(["job", "queue", name, ready], session=sid)
    if q1.returncode != 0:
        raise Failure(f"queue ready job failed: {q1.stderr[-800:]}")
    q2 = run_foreman(["job", "queue", name, waiting], session=sid)
    if q2.returncode != 0:
        raise Failure(f"queue second job failed: {q2.stderr[-800:]}")
    log = _clear_launch_log(world)
    tick = run_foreman(["collector", "once"])
    if tick.returncode != 0:
        raise Failure(f"collector once failed: {tick.stderr[-800:]}")
    recorded = log.read_text(encoding="utf-8")
    if "--headless" not in recorded:
        raise Failure(
            f"fake launcher did not record --headless:\n{recorded[-1500:]}")
    listed = run_foreman(["job", "list", name], session=sid)
    if listed.returncode != 0:
        raise Failure(f"job list failed: {listed.stderr[-800:]}")
    out = listed.stdout
    if f"{ready}  builder  muse  running " not in out \
            and f"{ready}" not in out:
        raise Failure(f"job list missing running {ready}:\n{out[-800:]}")
    if "running" not in out:
        raise Failure(f"job list did not show running:\n{out[-800:]}")
    if f"{waiting}" not in out or "no slot" not in out:
        raise Failure(
            f"second job did not wait no slot:\n{out[-800:]}")
    return (
        f"tick started {ready} with --headless; "
        f"{waiting} waits no slot; job list shows running"
    )
