"""Move a v1 state directory forward to what v2 changed.

Two record shapes grew on the v2 branch, both read-tolerant (older
lines still load) but write-explicit (new lines always carry them):

- front lines gained ``merge`` ("" from the desk discipline, "self"
  for a front that lands itself). A v1 line carries no ``merge`` key
  at all; the runtime defaults it to "self", and this migration writes
  that default down, appending a revised copy the way every ledger
  change here is made.
- merge lines gained ``front``. A v1 line names only its branch, tasks
  and target; the front is recovered from the tasks it lands, which
  each know their front. A merge whose tasks point at no single front
  cannot be repaired: it prints a notice and stays as it is, and the
  migration still exits 0 so nothing queued behind it is blocked.

Standard library only, no Foreman imports: this runs as a script from
an installed package too. Idempotent by guard — a re-run appends
nothing — and exit 0 in every case but a missing permission, which
stays pending for a re-run from a terminal that has it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

#: What a front without a merge mode lands as: itself. The merge desk
#: is a product swarm's discipline; a front that declares no desk must
#: not be stopped from landing by one.
DEFAULT_MERGE_MODE = "self"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_ledger(path: Path) -> list[dict]:
    """Ledger lines as dicts, blank lines dropped.

    A torn final line (a crash mid-append) reads as absent, the way the
    runtime reads it; anything else undecodable is a real error.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    raw_lines = [line for line in text.split("\n") if line.strip()]
    records = []
    for index, line in enumerate(raw_lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(raw_lines) - 1:
                continue
            raise
        if isinstance(record, dict):
            records.append(record)
    return records


def fold_by_id(records: list[dict]) -> list[dict]:
    order: list[str] = []
    by_id: dict[str, dict] = {}
    for record in records:
        rid = record.get("id")
        if not isinstance(rid, str) or not rid:
            continue
        if rid not in by_id:
            order.append(rid)
        by_id[rid] = record
    return [by_id[rid] for rid in order]


def append_ledger(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def front_names(state: Path) -> list[str]:
    try:
        return sorted(entry.name for entry in (state / "fronts").iterdir()
                      if entry.is_dir())
    except OSError:
        return []


def migrate_fronts(state: Path, now: str) -> int:
    """Backfill ``merge: "self"`` on pre-v2 front lines. Returns appends."""
    appends = 0
    for name in front_names(state):
        path = state / "fronts" / name / "front.jsonl"
        for record in fold_by_id(read_ledger(path)):
            if "merge" in record:
                continue
            if not record.get("id"):
                print(f"migration: front '{name}' has a line with no id; "
                      f"left alone")
                continue
            append_ledger(path, dict(record, merge=DEFAULT_MERGE_MODE,
                                     at=now, by="owner"))
            appends += 1
            print(f"migration: front '{name}' gains merge "
                  f"'{DEFAULT_MERGE_MODE}'")
    return appends


def task_fronts(state: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Task id -> front, and task title -> fronts holding that title."""
    by_id: dict[str, str] = {}
    by_title: dict[str, list[str]] = {}
    for name in front_names(state):
        path = state / "fronts" / name / "tasks.jsonl"
        for record in fold_by_id(read_ledger(path)):
            rid = record.get("id")
            if isinstance(rid, str) and rid and rid not in by_id:
                by_id[rid] = name
            title = record.get("title")
            if isinstance(title, str) and title:
                by_title.setdefault(title, [])
                if name not in by_title[title]:
                    by_title[title].append(name)
    return by_id, by_title


def resolve_front(refs: list, by_id: dict, by_title: dict) -> str | None:
    """The one front owning every referenced task, or None."""
    candidates: set[str] = set()
    for ref in refs:
        if not (isinstance(ref, str) and ref.strip()):
            return None
        key = ref.strip()
        if key in by_id:
            candidates.add(by_id[key])
            continue
        fronts = by_title.get(key, [])
        if len(fronts) != 1:
            return None
        candidates.add(fronts[0])
    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def migrate_merges(state: Path, now: str) -> int:
    """Backfill ``front`` on v1 merge lines where the tasks name one."""
    path = state / "merges.jsonl"
    records = fold_by_id(read_ledger(path))
    if not records:
        return 0
    by_id, by_title = task_fronts(state)
    appends = 0
    for record in records:
        if record.get("front"):
            continue
        mid = record.get("id") or "(no id)"
        front = resolve_front(record.get("tasks") or [], by_id, by_title)
        if front is None:
            print(f"migration: merge '{mid}' names no single front "
                  f"(tasks {record.get('tasks')}); left without one — "
                  f"the desk shows it as '(no front)'")
            continue
        append_ledger(path, dict(record, front=front, at=now, by="owner"))
        appends += 1
        print(f"migration: merge '{mid}' gains front '{front}'")
    return appends


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--config-dir", required=True)
    args = parser.parse_args()
    state = Path(args.state_dir)
    now = utcnow_iso()
    fronts = migrate_fronts(state, now)
    merges = migrate_merges(state, now)
    if not fronts and not merges:
        print("migration 1788902590: nothing to do")
    else:
        print(f"migration 1788902590: {fronts} front line(s), "
              f"{merges} merge line(s) moved to v2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
