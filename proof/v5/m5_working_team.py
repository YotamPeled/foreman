"""Clause 5.5: derived team under the ceiling; pool-out raises the other builder."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAUSE = ("5.5", "working_team")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "team.py"
_NEEDLE = (
    "                count = min(wanted, ceiling)\n"
    "                reason = _reason_leaves(leaf_n)\n"
)
_PATCH = (
    "                count = min(ceiling, ceiling)\n"
    "                reason = _reason_leaves(leaf_n)\n"
)


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
        raise Failure("BREAK apply: derive_team builder min(wanted, ceiling) moved")
    world["_55_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_55_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = (
    "derive_team uses the ceiling instead of the derived builder count",
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


def _front(name: str) -> dict:
    rows = _rows(_state() / "fronts" / name / "front.jsonl")
    if not rows:
        raise Failure(f"no front record for {name}")
    return rows[-1]


def _ok(argv: list[str]) -> str:
    proc = run_foreman(argv)
    if proc.returncode != 0:
        raise Failure(
            f"foreman {' '.join(argv[:4])} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[-800:]}")
    return proc.stdout


def _write_front(name: str) -> None:
    directory = _state() / "fronts" / name
    directory.mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "grok-4.6", "pool": "grok", "model": "grok-4.6",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "grok-4.6", "pool": "grok", "model": "grok-4.6",
         "effort": "high", "count": 3, "role": "builder"},
        {"agent": "muse", "pool": "muse", "model": "muse",
         "effort": "high", "count": 3, "role": "builder"},
        {"agent": "opus-5", "pool": "claude", "model": "claude-opus-5",
         "effort": "high", "count": 1, "role": "backup-builder"},
        {"agent": "astra-6", "pool": "codex", "model": "gpt-6-astra",
         "effort": "low", "count": 1, "role": "reviewer"},
    ]
    line = {
        "id": f"frt-{name}", "name": name, "state": "queued", "shape": "v5",
        "goal": "Derive the working team.",
        "finish_line": "The working team is derived.",
        "allocation": {"grok": 3, "muse": 3, "opus": 1, "astra": 1},
        "team": team,
        "supervisor": {"agent": "grok-4.6", "pool": "grok",
                       "model": "grok-4.6", "effort": "high"},
    }
    with (directory / "front.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _write_leaves(name: str, n: int) -> None:
    path = _state() / "fronts" / name / "tree.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "id": "mil-1", "front": name, "parent": name,
            "kind": "milestone", "title": "next", "state": "",
        }) + "\n")
        for i in range(n):
            handle.write(json.dumps({
                "id": f"job-{i + 1:02d}", "front": name, "parent": "mil-1",
                "kind": "job", "title": f"leaf {i + 1}",
            }) + "\n")


def _builder(record: dict, pool: str) -> dict:
    working = record.get("working_team") or []
    found = [e for e in working
             if isinstance(e, dict) and e.get("role") == "builder"
             and e.get("pool") == pool]
    if not found:
        raise Failure(f"no builder on {pool} in {working}")
    return found[0]


def run(world) -> str:
    assert_world()
    n = world.get("_55_n", 0) + 1
    world["_55_n"] = n
    name = f"wt{n}"
    _write_front(name)
    _write_leaves(name, 4)

    out = _ok(["front", "team", name])
    if "builder grok: 2 of 3 (4 leaves in the next milestone)" not in out:
        raise Failure(f"front team did not derive grok 2 of 3:\n{out}")
    record = _front(name)
    grok = _builder(record, "grok")
    if not (grok.get("count") < 3 and grok.get("count") == 2
            and grok.get("ceiling") == 3):
        raise Failure(f"derived grok builder is {grok}")
    if "4 leaves in the next milestone" not in str(grok.get("reason") or ""):
        raise Failure(f"reason is {grok.get('reason')!r}")

    reserved = _ok(["front", "reserve", name])
    if "reserved grok 2 (builders)" not in reserved:
        raise Failure(f"reservation did not hold derived 2:\n{reserved}")
    recs = [r for r in _fold(_rows(_state() / "reservations.jsonl")).values()
            if r.get("front") == name and r.get("pool") == "grok"
            and r.get("role") == "grok" and not r.get("released_at")]
    if len(recs) != 1 or recs[0].get("count") != 2:
        raise Failure(f"grok builders reservation was {recs}")

    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with (_state() / "pools.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "id": "grok", "pool": "grok", "out_until": until,
            "because": "quota",
        }) + "\n")
    before = _builder(_front(name), "muse").get("count")
    tick = run_foreman(["collector", "once"])
    if tick.returncode != 0:
        raise Failure(f"collector once failed: {tick.stderr[-800:]}")
    after = _builder(_front(name), "muse")
    if after.get("count") != (before if isinstance(before, int) else 0) + 1:
        raise Failure(
            f"pool-out did not raise muse by one "
            f"({before!r} -> {after.get('count')!r})")
    if after.get("count") > after.get("ceiling"):
        raise Failure(f"muse rose past the ceiling: {after}")
    claims = [str(row.get("claim") or "")
              for row in _rows(_state() / "fronts" / name / "evidence.jsonl")]
    needle = "team adjusted: builder muse "
    if not any(needle in claim and "(pool out)" in claim for claim in claims):
        raise Failure(f"no pool-out evidence line in {claims}")
    return (
        f"front {name} derived grok 2 of 3 (4 leaves in the next milestone); "
        f"reservation held 2; pool-out raised muse {before} -> "
        f"{after.get('count')} and wrote the evidence line"
    )
