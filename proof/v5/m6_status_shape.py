"""Clause 6.1: status prints the v5 queue, pieces, tree, landings and behind."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

CLAUSE = ("6.1", "status_shape")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "status.py"
_NEEDLE = (
    '            f"    M{number} {title}: {pieces} pieces "\n'
    '            f"(split from {split_from})")\n'
)
_PATCH = (
    '            f"    M{number} {title}: (split from {split_from})")\n'
)

_NODE = {
    "repo": "foreman",
    "verify": "python -m pytest tests -q",
    "must_not_touch": "the live state directory",
    "reason": "the tree needs this node",
    "break": "drop the pieces count",
}

_BEHIND = "abcdef1234567890abcdef1234567890"


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
        raise Failure("BREAK apply: milestone pieces print line moved")
    world["_61_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_61_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = (
    "the milestone line omits the landed/leaves pieces count",
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


def _import_running(world, front: str, n: int) -> str:
    sid = _field(_ok(["register", "--role", "supervisor", "--front", front,
                      "--pid", str(os.getpid())]), "session:")
    directory = Path(world["specs"]) / f"m6-shape-{n}"
    directory.mkdir(parents=True, exist_ok=True)
    mil, tsk_a, tsk_b = f"mil-s{n}", f"tsk-s{n}a", f"tsk-s{n}b"
    job, land = f"job-s{n}", f"job-land{n}"
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
    return sid


def _queue_block(out: str) -> list[str]:
    lines = out.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == "queue:" or line.startswith("queue: "):
            start = index
            break
    if start is None:
        raise Failure(f"no queue: block in:\n{out[-2000:]}")
    body = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("  ") and line != "":
            break
        if line.startswith("  "):
            body.append(line)
    return body


def run(world) -> str:
    assert_world()
    n = world.get("_61_n", 0) + 1
    world["_61_n"] = n
    top, nxt = f"sa{n}", f"sb{n}"
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
    queue = _queue_block(status)
    if queue[:3] != [
            "queue:",
            f"  {top}  team fits",
            f"  {nxt}  behind {top}"]:
        raise Failure(f"queue block was {queue!r}")
    if "held 0 / reserved 1 / cap 3" not in status:
        raise Failure(
            f"status missing held 0 / reserved 1 / cap 3:\n{status[-2000:]}")
    pieces = re.search(r"M\d+ The screen: (\d+)/(\d+) pieces", status)
    if pieces is None:
        raise Failure(f"no milestone pieces line in:\n{status[-2000:]}")
    if pieces.group(1) != "1" or pieces.group(2) != "2":
        raise Failure(
            f"milestone pieces were {pieces.group(1)}/{pieces.group(2)}, "
            "not 1/2")
    job = f"job-s{n}"
    land = f"job-land{n}"
    tree_hit = False
    for line in status.splitlines():
        if job in line and "waits: ready" in line:
            tree_hit = True
            indent = len(line) - len(line.lstrip(" "))
            if indent < 6:
                raise Failure(
                    f"queued job was not indented by depth: {line!r}")
            break
    if not tree_hit:
        raise Failure(f"tree never named {job} with waits: ready:\n{status[-2000:]}")
    if "    landings:" not in status:
        raise Failure(f"no landings: block in:\n{status[-2000:]}")
    if f"{land}  queued" not in status:
        raise Failure(f"landings never named {land} queued:\n{status[-2000:]}")
    if "behind main (abcdef1)" not in status:
        raise Failure(
            f"missing behind main (abcdef1):\n{status[-2000:]}")
    return (
        f"status queue named {top} team fits and {nxt} behind {top}; "
        f"fake held 0 / reserved 1 / cap 3; M-line 1/2 pieces; "
        f"tree waits: ready on {job}; landings {land} queued; "
        f"behind main (abcdef1)"
    )
