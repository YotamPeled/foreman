"""Finish-line f1: this repository's fronts/v5/ is held by the runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

_NODE_STATES = ("queued", "running", "returned", "verified", "landed",
                "failed", "cancelled", "redesign")

CLAUSE = ("f1", "held by runtime")


def _checkout() -> Path:
    here = Path(__file__).resolve()
    candidates = []
    if len(here.parents) > 2:
        candidates.append(here.parents[2])
    candidates.append(Path.cwd())
    for candidate in candidates:
        if (candidate / "src" / "foreman").is_dir() and (
                candidate / "fronts" / "v5").is_dir():
            return candidate
    return Path.cwd()


_REPO = _checkout()
_V5_FILES = _REPO / "fronts" / "v5"
_SRC = _REPO / "src" / "foreman" / "node.py"
_NEEDLE = (
    "    line = f\"{indent}{node.get('id')}  {node.get('kind')}  "
    "{node.get('title')}\"\n"
)
_PATCH = (
    "    line = f\"{indent}{node.get('kind')}  {node.get('title')}\"\n"
)
_SEEN_AT = "2026-09-10T23:00:00+00:00"

_V5 = '''\
name = "v5"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front's map, milestones and tree are held by the runtime."
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


def _jsonl_ids(path: Path) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        nid = row.get("id")
        if isinstance(nid, str) and nid and nid not in seen:
            seen.add(nid)
            found.append(nid)
    return found


def _missing(ids: list[str], out: str) -> list[str]:
    return [nid for nid in ids if f"{nid}  " not in out and nid not in out]


def _bare(world) -> Path:
    stored = world.get("_f1_bare")
    if stored is not None:
        return Path(stored)
    root = Path(world["repo_a"]).parent / "f1-held"
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
    world["_f1_bare"] = str(bare)
    return bare


def _commit() -> str:
    proc = subprocess.run(
        ["git", "-C", str(_REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True)
    sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or not sha:
        raise Failure("could not read HEAD of this checkout")
    return sha


def _import_dir(world) -> Path:
    """A copy of fronts/v5/ whose tree lines only carry node states.

    The live tree file records milestone ``state: built``, which the
    node door refuses. Import of the copy still holds every id.
    """
    dest = Path(world["specs"]) / "f1-v5-files"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(_V5_FILES, dest)
    tree = dest / "tree.jsonl"
    rows = []
    for raw in tree.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        state = row.get("state")
        if isinstance(state, str) and state and state not in _NODE_STATES:
            row = dict(row)
            row.pop("state", None)
        rows.append(row)
    tree.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return dest


def _ensure_front(world) -> str:
    sid = world.get("_f1_sid")
    if isinstance(sid, str) and sid:
        shown = run_foreman(["front", "show", "v5"])
        if shown.returncode == 0:
            return sid
    bare = _bare(world)
    directory = Path(world["specs"]) / "f1-v5"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(url=str(bare)), encoding="utf-8")
    added = run_foreman(["front", "add", str(directory)])
    if added.returncode != 0:
        raise Failure(f"front add v5 failed: {added.stderr[-800:]}")
    proc = run_foreman(
        ["register", "--role", "supervisor", "--front", "v5",
         "--pid", str(os.getpid())])
    if proc.returncode != 0:
        raise Failure(f"register supervisor failed: {proc.stderr[-800:]}")
    sid = _field(proc.stdout, "session:")
    world["_f1_sid"] = sid
    return sid


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_f1_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_f1_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "node list omits each node's id so the imported tree ids disappear",
    apply, restore,
)


def run(world) -> str:
    assert_world()
    if not _V5_FILES.is_dir():
        raise Failure(f"missing {_V5_FILES}")
    sid = _ensure_front(world)
    imported = run_foreman(
        ["front", "import", "v5", str(_import_dir(world)),
         "--seen-at", _SEEN_AT, "--commit", _commit()],
        session=sid)
    if imported.returncode != 0:
        raise Failure(f"front import failed: {imported.stderr[-800:]}")

    mil_ids = _jsonl_ids(_V5_FILES / "milestones.jsonl")
    node_ids = _jsonl_ids(_V5_FILES / "tree.jsonl")
    if not mil_ids or not node_ids:
        raise Failure("fronts/v5 files hold no milestone or node ids")

    shown = run_foreman(["map", "show", "v5"], session=sid)
    if shown.returncode != 0:
        raise Failure(f"map show failed: {shown.stderr[-800:]}")
    if "(no map yet)" in shown.stdout or not shown.stdout.strip():
        raise Failure(f"map show printed no facts:\n{shown.stdout[-800:]}")
    as_json = run_foreman(["map", "show", "v5", "--json"], session=sid)
    if as_json.returncode != 0:
        raise Failure(f"map show --json failed: {as_json.stderr[-800:]}")
    try:
        facts = json.loads(as_json.stdout)
    except json.JSONDecodeError as exc:
        raise Failure(f"map show --json is not JSON: {exc}") from exc
    if not isinstance(facts, list) or not facts:
        raise Failure("map show --json printed no facts")
    map_ids = [row.get("id") for row in facts
               if isinstance(row, dict) and row.get("id")]
    missing_map = [fid for fid in map_ids if not isinstance(fid, str)]
    if missing_map or len(map_ids) != len(facts):
        raise Failure("map show --json omitted a fact id")

    listed_m = run_foreman(["milestone", "list", "v5"], session=sid)
    if listed_m.returncode != 0:
        raise Failure(f"milestone list failed: {listed_m.stderr[-800:]}")
    miss_m = _missing(mil_ids, listed_m.stdout)
    if miss_m:
        raise Failure(
            f"milestone list missed {miss_m[:8]}: {listed_m.stdout[-800:]}")

    listed_n = run_foreman(["node", "list", "v5"], session=sid)
    if listed_n.returncode != 0:
        raise Failure(f"node list failed: {listed_n.stderr[-800:]}")
    miss_n = _missing(node_ids, listed_n.stdout)
    if miss_n:
        raise Failure(
            f"node list missed {miss_n[:8]}: {listed_n.stdout[-800:]}")
    return (
        f"import held {len(map_ids)} map facts, {len(mil_ids)} milestones, "
        f"{len(node_ids)} nodes; map show, milestone list and node list "
        f"print every id the files hold"
    )
