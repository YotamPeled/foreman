"""Clause 6.2: panel-feed writes the v5 blocks as data, agreeing with status."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

CLAUSE = ("6.2", "panel_feed")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "panel_feed.py"
_NEEDLE = '        "tree": _tree_rows(name),\n'
_PATCH = '        "tree": [],\n'

_NODE = {
    "repo": "foreman",
    "verify": "python -m pytest tests -q",
    "must_not_touch": "the live state directory",
    "reason": "the tree needs this node",
    "break": "drop the pieces count",
}

_BEHIND = "abcdef1234567890abcdef1234567890"

_NUMBER_KEYS = (
    "count", "landed", "leaves", "split_from", "depth", "held", "reserved",
    "cap",
)
_TIME_KEYS = ("at", "now")


def _drop_pyc(path: Path) -> None:
    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return
    for item in cache.iterdir():
        if item.name.startswith(path.stem + "."):
            item.unlink(missing_ok=True)


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure("BREAK apply: tree block add line moved")
    world["_62_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_62_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = (
    "the tree block added to the feed is an empty list",
    apply, restore,
)


def _state() -> Path:
    return Path(os.environ["FOREMAN_STATE"])


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _fold(rows: list[dict]) -> dict[str, dict]:
    by: dict[str, dict] = {}
    for row in rows:
        key = row.get("id")
        if isinstance(key, str) and key:
            by[key] = row
    return by


def _ok(argv: list[str], session: str | None = None) -> str:
    proc = run_foreman(argv, session=session)
    if proc.returncode != 0:
        raise Failure(
            f"foreman {' '.join(argv[:4])} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[-800:]}")
    return proc.stdout


def _field(out: str, prefix: str) -> str:
    needle = prefix if prefix.endswith(" ") else prefix + " "
    for line in out.splitlines():
        if line.startswith(needle):
            return line[len(needle):].strip()
    raise Failure(f"no {prefix!r} line in output:\n{out[-2000:]}")


def _retire_queued() -> None:
    root = _state() / "fronts"
    if not root.is_dir():
        return
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        path = directory / "front.jsonl"
        rows = _rows(path)
        if not rows:
            continue
        last = dict(rows[-1])
        if last.get("state") == "queued":
            last["state"] = "done"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(last) + "\n")
    path = _state() / "reservations.jsonl"
    folded = _fold(_rows(path))
    extra = [dict(rec, released_at="2026-09-10T12:00:00+00:00")
             for rec in folded.values() if not rec.get("released_at")]
    if extra:
        with path.open("a", encoding="utf-8") as handle:
            for rec in extra:
                handle.write(json.dumps(rec) + "\n")


def _write_v5(name: str, *, state: str, behind: str, requested_at: str,
              builders: int = 1) -> None:
    directory = _state() / "fronts" / name
    directory.mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "fake", "pool": "fake", "model": "fake-test-model",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "fake", "pool": "fake", "model": "fake-test-model",
         "effort": "high", "count": builders, "role": "builder"},
    ]
    line = {
        "id": f"frt-{name}", "name": name, "state": state, "shape": "v5",
        "prefer": 0, "requested_at": requested_at, "behind": behind,
        "goal": "Show the v5 screen.",
        "finish_line": "The queue names both.",
        "allocation": {"fake": builders}, "team": team,
        "repositories": [{"name": "foreman", "target": "main",
                          "work": name, "base": "main",
                          "url": "https://example.invalid/foreman.git"}],
        "supervisor": {"agent": "fake", "pool": "fake",
                       "model": "fake-test-model", "effort": "high"},
    }
    with (directory / "front.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _append_tree(front: str, record: dict) -> None:
    path = _state() / "fronts" / front / "tree.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _import_running(world, front: str, n: int) -> None:
    sid = _field(_ok(["register", "--role", "supervisor", "--front", front,
                      "--pid", str(os.getpid())]), "session:")
    directory = Path(world["specs"]) / f"m6-feed-{n}"
    directory.mkdir(parents=True, exist_ok=True)
    mil, tsk_a, tsk_b = f"mil-f{n}", f"tsk-f{n}a", f"tsk-f{n}b"
    job, land = f"job-f{n}", f"job-fland{n}"
    (directory / "milestones.jsonl").write_text(
        json.dumps({
            "id": mil, "title": "The screen",
            "done_when": "status shows the v5 shape",
            "verify": "python -m pytest tests -q",
            "reason": "decision 15", "break": "drop the pieces count",
            "order": 1,
        }) + "\n", encoding="utf-8")
    nodes = [
        {"id": mil, "parent": front, "kind": "milestone",
         "title": "The screen", **_NODE},
        {"id": tsk_a, "parent": mil, "kind": "task",
         "title": "first piece", **_NODE},
        {"id": tsk_b, "parent": mil, "kind": "task",
         "title": "second piece", **_NODE},
        {"id": job, "parent": tsk_a, "kind": "job",
         "title": "implement it", "role": "builder", **_NODE},
        {"id": land, "parent": mil, "kind": "job",
         "title": "land implement it", "role": "script", **_NODE},
    ]
    (directory / "tree.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in nodes), encoding="utf-8")
    imported = run_foreman(
        ["front", "import", front, str(directory)], session=sid)
    if imported.returncode != 0:
        raise Failure(f"front import failed: {imported.stderr[-800:]}")
    folded = _fold(_rows(_state() / "fronts" / front / "tree.jsonl"))
    for nid, extra in (
            (tsk_a, {"state": "landed"}),
            (tsk_b, {"state": "queued"}),
            (job, {"state": "queued", "waits": "ready"}),
            (land, {"state": "queued", "role": "script", "lands": job,
                    "waits": "ready"}),
    ):
        rec = dict(folded[nid])
        rec.update(extra)
        _append_tree(front, rec)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_iso(value: object, where: str) -> None:
    if not isinstance(value, str) or not value:
        return
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise Failure(f"{where} {value!r} does not parse as ISO") from exc


def _check_row_types(row: dict, where: str) -> None:
    if not isinstance(row, dict):
        raise Failure(f"{where} is not an object")
    for key, value in row.items():
        if key in _NUMBER_KEYS and value is not None and not _is_number(value):
            raise Failure(f"{where}.{key} is {value!r}, not a number")
        if isinstance(value, dict) and key == "reserved":
            for pool, count in value.items():
                if not _is_number(count):
                    raise Failure(
                        f"{where}.reserved.{pool} is {count!r}, not a number")
        if key in _TIME_KEYS or key.endswith("_at") or key.endswith("At"):
            _parse_iso(value, f"{where}.{key}")


def _front_row(feed: dict, name: str) -> dict:
    for row in feed.get("fronts") or []:
        if isinstance(row, dict) and row.get("name") == name:
            return row
    raise Failure(f"panel.json fronts has no {name!r} row")


def _status_tree_ids(out: str) -> list[str]:
    lines = out.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == "    tree:":
            start = index + 1
            break
    if start is None:
        raise Failure(f"no tree: block in status:\n{out[-2000:]}")
    ids = []
    for line in lines[start:]:
        if not line.startswith("    "):
            break
        rest = line[4:]
        if rest.startswith("M") and "pieces" in rest:
            break
        if rest.startswith("landings:") or rest.startswith("want:"):
            break
        stripped = rest.lstrip()
        if stripped.startswith("(no tree"):
            continue
        nid = stripped.split(None, 1)[0] if stripped else ""
        if nid:
            ids.append(nid)
    return ids


def run(world) -> str:
    assert_world()
    n = world.get("_62_n", 0) + 1
    world["_62_n"] = n
    top, nxt = f"fa{n}", f"fb{n}"
    _retire_queued()
    capped = run_foreman(["cap", "fake", "3"])
    if capped.returncode != 0:
        raise Failure(f"cap fake 3 failed: {capped.stderr[-800:]}")
    _write_v5(top, state="queued", behind=_BEHIND,
              requested_at="2026-09-10T12:00:00+00:00")
    _write_v5(nxt, state="queued", behind="",
              requested_at="2026-09-10T12:00:01+00:00")
    _import_running(world, top, n)
    reserved = run_foreman(["front", "reserve", top, "--phase", "builders"])
    if reserved.returncode != 0:
        raise Failure(f"front reserve failed: {reserved.stderr[-800:]}")

    status = _ok(["status"])
    fed = _ok(["panel-feed"]).strip()
    path = Path(fed)
    if not path.is_file():
        path = _state() / "panel.json"
    if not path.is_file():
        raise Failure(f"panel-feed wrote no panel.json (printed {fed!r})")
    try:
        feed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Failure(f"panel.json is not JSON: {exc}") from exc
    if not isinstance(feed, dict):
        raise Failure("panel.json is not an object")

    for key in ("front_queue", "capacity", "verbs"):
        if key not in feed:
            raise Failure(f"panel.json has no {key}")
    queue = feed["front_queue"]
    if not isinstance(queue, dict) or not isinstance(queue.get("rows"), list):
        raise Failure(f"front_queue is {queue!r}")
    _check_row_types(queue, "front_queue")
    for index, row in enumerate(queue["rows"]):
        _check_row_types(row, f"front_queue.rows[{index}]")
    names = [row.get("front") for row in queue["rows"]]
    if top not in names or nxt not in names:
        raise Failure(f"front_queue rows {names} missing {top} or {nxt}")

    capacity = feed["capacity"]
    if not isinstance(capacity, dict) or not isinstance(capacity.get("rows"), list):
        raise Failure(f"capacity is {capacity!r}")
    _check_row_types(capacity, "capacity")
    fake = None
    for index, row in enumerate(capacity["rows"]):
        _check_row_types(row, f"capacity.rows[{index}]")
        if row.get("pool") == "fake":
            fake = row
    if fake is None:
        raise Failure(f"capacity rows have no fake pool: {capacity['rows']!r}")
    if not all(_is_number(fake.get(k)) for k in ("held", "reserved", "cap")):
        raise Failure(f"fake capacity numbers were {fake}")

    front = _front_row(feed, top)
    for key in ("milestones", "tree", "landings"):
        rows = front.get(key)
        if not isinstance(rows, list):
            raise Failure(f"front {top} {key} is {rows!r}")
        for index, row in enumerate(rows):
            _check_row_types(row, f"fronts.{top}.{key}[{index}]")
    _parse_iso(feed.get("at"), "at")
    _parse_iso(feed.get("now"), "now")

    mil = (front.get("milestones") or [None])[0]
    if not isinstance(mil, dict):
        raise Failure(f"{top} milestones were {front.get('milestones')!r}")
    pieces = re.search(r"M\d+ The screen: (\d+)/(\d+) pieces", status)
    if pieces is None:
        raise Failure(f"status had no pieces line:\n{status[-2000:]}")
    if (mil.get("landed"), mil.get("leaves")) != (
            int(pieces.group(1)), int(pieces.group(2))):
        raise Failure(
            f"feed landed/leaves {mil.get('landed')}/{mil.get('leaves')} "
            f"!= status {pieces.group(1)}/{pieces.group(2)}")
    tree_ids = [row.get("id") for row in front.get("tree") or []
                if isinstance(row, dict)]
    status_ids = _status_tree_ids(status)
    if tree_ids != status_ids:
        raise Failure(
            f"feed tree ids {tree_ids} != status tree ids {status_ids}")

    listed = _ok(["mcp", "--list-verbs"]).splitlines()
    verbs = feed.get("verbs")
    if verbs != listed:
        raise Failure(
            f"panel.json verbs ({len(verbs or [])}) != mcp --list-verbs "
            f"({len(listed)})")
    if "job_queue" not in listed or "node_add" not in listed:
        raise Failure(f"mcp --list-verbs missed job_queue or node_add: {listed[:20]}")
    return (
        f"panel.json front_queue named {top} and {nxt}; capacity fake "
        f"held {fake.get('held')} / reserved {fake.get('reserved')} / "
        f"cap {fake.get('cap')}; {top} milestones {mil.get('landed')}/"
        f"{mil.get('leaves')} matched status; tree ids {tree_ids} matched; "
        f"verbs listed {len(listed)} names from mcp --list-verbs"
    )
