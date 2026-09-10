"""Clause 3.5: a held resource is why the second queued job waits."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUSE = ("3.5", "resources")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "progress.py"
_NEEDLE = "                return f\"resource {name}\"\n"
_PATCH = "                return \"ready\"\n"

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
    stored = world.get("_35_bare")
    if stored is not None:
        return Path(stored), Path(world["_35_git"])
    root = Path(world["repo_a"]).parent / "m3-res"
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
    world["_35_bare"] = str(bare)
    world["_35_git"] = str(src)
    return bare, src


def _add_front(world, name: str, url: str, work: str) -> None:
    directory = Path(world["specs"]) / f"m3-res-{name}"
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
    for item in flags.get("resources") or []:
        argv.extend(["--resource", item])
    proc = run_foreman(argv, session=sid)
    if proc.returncode != 0:
        raise Failure(
            f"node add {kind} {title!r} failed: {proc.stderr[-800:]}")
    return proc.stdout.strip()


def _ensure_fixture_db() -> None:
    path = Path(os.environ["FOREMAN_CONFIG"]) / "foreman.toml"
    text = path.read_text(encoding="utf-8")
    if "fixture-db" in text:
        return
    path.write_text(text + "\n[resources]\nfixture-db = 1\n", encoding="utf-8")


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_35_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_35_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "treat a held resource as ready so the second job does not wait",
    apply, restore,
)


def run(world) -> str:
    assert_world()
    isolate_queued_fronts()
    _ensure_fixture_db()
    bare, src = _bare(world)
    n = world.get("_35_n", 0) + 1
    world["_35_n"] = n
    name = f"rs{n}"
    work = f"w{name}"
    _add_front(world, name, str(bare), work)
    _ensure_work(src, bare, work)
    reserved = run_foreman(["front", "reserve", name, "--phase", "builders"])
    if reserved.returncode != 0:
        raise Failure(f"front reserve {name} failed: {reserved.stderr[-800:]}")
    sid = _register(name)
    mil = _add_ok(name, sid, name, "milestone", "milestone one",
                  node_id=f"mil-rs{n}")
    tsk = _add_ok(name, sid, mil, "task", "the door",
                  node_id=f"tsk-rs{n}")
    first = _add_ok(name, sid, tsk, "job", "use the db",
                    role="builder", node_id=f"job-rsa{n}",
                    resources=["fixture-db"])
    second = _add_ok(name, sid, tsk, "job", "the next unit",
                     role="builder", node_id=f"job-rsb{n}",
                     resources=["fixture-db"])
    q1 = run_foreman(["job", "queue", name, first], session=sid)
    if q1.returncode != 0:
        raise Failure(f"queue first failed: {q1.stderr[-800:]}")
    q2 = run_foreman(["job", "queue", name, second], session=sid)
    if q2.returncode != 0:
        raise Failure(f"queue second failed: {q2.stderr[-800:]}")
    tick = run_foreman(["collector", "once"])
    if tick.returncode != 0:
        raise Failure(f"collector once failed: {tick.stderr[-800:]}")
    listed = run_foreman(["job", "list", name], session=sid)
    if listed.returncode != 0:
        raise Failure(f"job list failed: {listed.stderr[-800:]}")
    if "running" not in listed.stdout:
        raise Failure(f"tick started neither job:\n{listed.stdout[-800:]}")
    if "resource fixture-db" not in listed.stdout:
        raise Failure(
            f"second job did not wait resource fixture-db:\n"
            f"{listed.stdout[-800:]}")
    resources = run_foreman(["resource", "list"])
    if resources.returncode != 0:
        raise Failure(f"resource list failed: {resources.stderr[-800:]}")
    if "fixture-db: held 1 / count 1" not in resources.stdout:
        raise Failure(
            f"resource list missing held 1 / count 1:\n"
            f"{resources.stdout[-800:]}")
    holder = f"  {name} {first}"
    if holder not in resources.stdout:
        raise Failure(
            f"resource list did not name holder {holder!r}:\n"
            f"{resources.stdout[-800:]}")
    return (
        f"tick started {first}; {second} waits resource fixture-db; "
        f"resource list names {name} {first}"
    )
