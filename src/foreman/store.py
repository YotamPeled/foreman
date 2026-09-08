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


@contextmanager
def _write_lock() -> Iterator[None]:
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


def write_snapshot(path: str | Path, obj: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with _write_lock():
            os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_snapshot(path: str | Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
