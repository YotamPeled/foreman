"""Clause 1.2: map add, show, resolve and import in the isolated world."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUSE = ("1.2", "map")

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

_THREE = """\
## Repository: foreman

### Storage

- store.py is the one writer **[seen]** (`src/foreman/store.py`)
- fold is last-wins **[seen]** (`src/foreman/store.py`)
- no database **[assumed]**
"""


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


def _bare(world) -> tuple[Path, str]:
    stored = world.get("_12_bare")
    if stored is not None:
        return Path(stored), world["_12_sha"]
    root = Path(world["repo_a"]).parent / "m1-map"
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
    world["_12_bare"] = str(bare)
    world["_12_sha"] = sha
    return bare, sha


def _add_front(world, name: str, url: str) -> None:
    directory = Path(world["specs"]) / f"map-{name}"
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


def apply(world) -> None:
    bare, _sha = _bare(world)
    proc = _git(bare, "branch", "v5")
    if proc.returncode != 0:
        raise Failure(f"BREAK apply could not create v5: {proc.stderr[-400:]}")


def restore(world) -> None:
    bare = world.get("_12_bare")
    if not bare:
        return
    _git(Path(bare), "branch", "-D", "v5")


BREAK = ("create the work branch on the remote so front add is refused",
         apply, restore)


def run(world) -> str:
    assert_world()
    bare, sha = _bare(world)
    n = world.get("_12_n", 0) + 1
    world["_12_n"] = n
    name = f"map{n}"
    _add_front(world, name, str(bare))
    sid = _register(name)
    added = run_foreman(
        ["map", "add", name, "--repo", "foreman", "--section", "Storage",
         "--fact", "store.py is the one writer",
         "--seen", "--where", "src/foreman/store.py"],
        session=sid)
    if added.returncode != 0:
        raise Failure(f"map add failed: {added.stderr[-800:]}")
    shown = run_foreman(["map", "show", name], session=sid)
    if shown.returncode != 0:
        raise Failure(f"map show failed: {shown.stderr[-800:]}")
    if "- [seen] store.py is the one writer" not in shown.stdout:
        raise Failure(f"map show did not print [seen]: {shown.stdout[-800:]}")
    resolved = run_foreman(
        ["map", "resolve", name, "--repo", "foreman", "--ref", "main"],
        session=sid)
    if resolved.returncode != 0:
        raise Failure(f"map resolve failed: {resolved.stderr[-800:]}")
    want = f"foreman main {sha}"
    if resolved.stdout.strip() != want:
        raise Failure(
            f"map resolve printed {resolved.stdout.strip()!r}, not {want!r}")
    path = Path(world["specs"]) / f"map-{name}-three.md"
    path.write_text(_THREE, encoding="utf-8")
    first = run_foreman(
        ["map", "import", name, str(path),
         "--seen-at", "2026-09-10T23:00:00+00:00", "--commit", sha],
        session=sid)
    if first.returncode != 0:
        raise Failure(f"map import first failed: {first.stderr[-800:]}")
    if "imported 3 facts" not in first.stdout:
        raise Failure(f"map import did not report 3: {first.stdout[-400:]}")
    second = run_foreman(
        ["map", "import", name, str(path),
         "--seen-at", "2026-09-10T23:00:00+00:00", "--commit", sha],
        session=sid)
    if second.returncode != 0:
        raise Failure(f"map import second failed: {second.stderr[-800:]}")
    if "imported 0 facts" not in second.stdout:
        raise Failure(f"map import did not report 0: {second.stdout[-400:]}")
    return (
        f"map add {added.stdout.strip()} [seen] on show; "
        f"resolve {sha[:7]}; import 3 then 0"
    )
