"""Clause 2.3: front import prints counts, is idempotent, stops on a job parent."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLAUSE = ("2.3", "front_import")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "front_import.py"
_NEEDLE = (
    "        code, _out, err = _call_door("
    "node.node_add_main, **_node_add_kwargs(front, line))\n"
    "        if code != 0:\n"
)
_PATCH = (
    "        code, _out, err = _call_door("
    "node.node_add_main, **_node_add_kwargs(front, line))\n"
    "        if False and code != 0:\n"
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

_MAP = """\
## Repository: foreman

### Storage

- store.py is the one writer **[seen]** (`src/foreman/store.py`)
- fold is last-wins **[seen]** (`src/foreman/store.py`)
- no database **[assumed]**
"""

_SEEN_AT = "2026-09-10T23:00:00+00:00"

_NODE_FIELDS = {
    "repo": "foreman",
    "verify": "python -m pytest tests -q",
    "must_not_touch": "the live state directory",
    "reason": "the tree needs this node",
    "break": "admit a job as a parent",
}


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


def _bare(world) -> tuple[Path, str]:
    stored = world.get("_23_bare")
    if stored is not None:
        return Path(stored), world["_23_sha"]
    root = Path(world["repo_a"]).parent / "m2-import"
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
    world["_23_bare"] = str(bare)
    world["_23_sha"] = sha
    return bare, sha


def _add_front(world, name: str, url: str) -> None:
    directory = Path(world["specs"]) / f"m2-imp-{name}"
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


def _front(world) -> tuple[str, str, str]:
    bare, sha = _bare(world)
    n = world.get("_23_n", 0) + 1
    world["_23_n"] = n
    name = f"im{n}"
    _add_front(world, name, str(bare))
    return name, _register(name), sha


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_23_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_23_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "skip _stop when node_add_main refuses a tree line",
    apply, restore,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _good_dir(world, front: str) -> Path:
    n = world["_23_n"]
    directory = Path(world["specs"]) / f"m2-imp-files-{n}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "map.md").write_text(_MAP, encoding="utf-8")
    _write_jsonl(directory / "milestones.jsonl", [
        {"id": "mil-a", "title": "First", "done_when": "first is done",
         "verify": "python -m pytest tests -q", "reason": "need first",
         "break": "drop first", "order": 1},
        {"id": "mil-b", "title": "Second", "done_when": "second is done",
         "verify": "python -m pytest tests -q", "reason": "need second",
         "break": "drop second", "order": 2},
    ])
    _write_jsonl(directory / "tree.jsonl", [
        {"id": "mil-a", "parent": front, "kind": "milestone",
         "title": "First", **_NODE_FIELDS},
        {"id": "tsk-a", "parent": "mil-a", "kind": "task",
         "title": "the door", **_NODE_FIELDS},
        {"id": "job-a", "parent": "tsk-a", "kind": "job",
         "title": "implement the door", **_NODE_FIELDS},
    ])
    return directory


def _bad_dir(world, front: str) -> Path:
    directory = _good_dir(world, front)
    tree = directory / "tree.jsonl"
    rows = tree.read_text(encoding="utf-8").splitlines()
    rows.append(json.dumps({
        "id": "bad-child", "parent": "job-a", "kind": "task",
        "title": "under a job", **_NODE_FIELDS,
    }))
    tree.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return directory


def _counts(out: str) -> dict[str, tuple[int, int, int]]:
    result: dict[str, tuple[int, int, int]] = {}
    for line in out.splitlines():
        if " new, " not in line or ":" not in line:
            continue
        name, rest = line.split(":", 1)
        parts = rest.strip().split(",")
        new = int(parts[0].strip().split()[0])
        present = int(parts[1].strip().split()[0])
        revised = int(parts[2].strip().split()[0])
        result[name.strip()] = (new, present, revised)
    return result


def _import_argv(front: str, directory: Path, sha: str) -> list[str]:
    return [
        "front", "import", front, str(directory),
        "--seen-at", _SEEN_AT, "--commit", sha,
    ]


def run(world) -> str:
    assert_world()
    name, sid, sha = _front(world)
    directory = _good_dir(world, name)
    first = run_foreman(_import_argv(name, directory, sha), session=sid)
    if first.returncode != 0:
        raise Failure(f"front import failed: {first.stderr[-800:]}")
    counts = _counts(first.stdout)
    if counts.get("map.md") != (3, 0, 0):
        raise Failure(f"first import map.md counts {counts.get('map.md')!r}, not (3, 0, 0)")
    if counts.get("milestones.jsonl") != (2, 0, 0):
        raise Failure(
            f"first import milestones.jsonl counts {counts.get('milestones.jsonl')!r}, "
            "not (2, 0, 0)")
    if counts.get("tree.jsonl") != (3, 0, 0):
        raise Failure(
            f"first import tree.jsonl counts {counts.get('tree.jsonl')!r}, not (3, 0, 0)")
    second = run_foreman(_import_argv(name, directory, sha), session=sid)
    if second.returncode != 0:
        raise Failure(f"second front import failed: {second.stderr[-800:]}")
    again = _counts(second.stdout)
    for key, total in (("map.md", 3), ("milestones.jsonl", 2), ("tree.jsonl", 3)):
        got = again.get(key)
        if got is None or got[0] != 0 or got[1] != total:
            raise Failure(
                f"second import {key} is {got!r}, not (0, {total}, _)")
    bad_name, bad_sid, bad_sha = _front(world)
    bad = _bad_dir(world, bad_name)
    stopped = run_foreman(_import_argv(bad_name, bad, bad_sha), session=bad_sid)
    if stopped.returncode == 0:
        raise Failure("import of a job parent was admitted")
    err = stopped.stderr or ""
    if "tree.jsonl" not in err:
        raise Failure(f"job-parent stop did not name tree.jsonl: {err[-800:]}")
    if "4" not in err:
        raise Failure(f"job-parent stop did not name the line: {err[-800:]}")
    if "job" not in err:
        raise Failure(f"job-parent stop did not name job: {err[-800:]}")
    return (
        f"import 2 milestones, 3 nodes, 3 facts; second 0 new; "
        f"job parent stopped naming tree.jsonl:4"
    )
