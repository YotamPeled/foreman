"""Disk state. The only module in the project that touches the state directory.

Concurrency: one lock file in the state directory, taken with fcntl.flock
and held only across a single write. Reads never take the lock; snapshots
stay consistent because the final os.replace is atomic.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import paths


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Set by every write below, read and cleared by the caller that refreshes
#: the panel's summary on its way out (see :mod:`foreman.panel_feed`). A verb
#: that only read — a dry run, a status, a refusal — leaves it false and
#: writes nothing at all, which is the contract those verbs are held to.
_wrote = False


def mark_written() -> None:
    """Record that the state directory changed under this process."""
    global _wrote
    _wrote = True


def take_written() -> bool:
    """True once per write, then false again until the next one."""
    global _wrote
    was, _wrote = _wrote, False
    return was


@contextmanager
def _write_lock() -> Iterator[None]:
    mark_written()
    lock = paths.lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def append_ledger(path: str | Path, record: dict, session_id: str | None = None) -> dict:
    entry = dict(record)
    entry.setdefault("at", utcnow_iso())
    if session_id is not None:
        entry.setdefault("by", session_id)
    line = json.dumps(entry) + "\n"
    ledger = Path(path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock():
        with open(ledger, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    return entry


def read_ledger(path: str | Path) -> list[dict]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    lines = [line for line in text.split("\n") if line.strip()]
    records = []
    for index, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                continue
            raise
    return records


def fold_by_id(records: list[dict]) -> list[dict]:
    """Fold an append-only ledger last-wins, keeping first-appearance order.

    Every ledger in the state directory is append-only: a record is changed
    by appending a revised copy of the whole line, never by editing a byte
    already written. Every reader therefore folds the same way, and this is
    the one place that says how. Lines with no id are not records and are
    dropped.
    """
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


def _replace_with(target: Path, obj: Any) -> None:
    """Stage the value beside the target and rename it into place."""
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_snapshot(path: str | Path, obj: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock():
        _replace_with(target, obj)


def update_snapshot(path: str | Path, change, default: Any = None) -> Any:
    """Read a snapshot, change it and write it back, all under one lock.

    Read-modify-write on a snapshot is not safe with write_snapshot alone:
    that holds the lock only across the rename, so two writers can both read
    the old value and the second one silently discards the first one's
    change. Every caller that edits a snapshot in place uses this instead.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock():
        try:
            with open(target, encoding="utf-8") as handle:
                current = json.load(handle)
        except FileNotFoundError:
            current = default
        changed = change(current)
        _replace_with(target, changed)
    return changed


def read_snapshot(path: str | Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
