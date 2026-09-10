"""`foreman wait <id> [<id> ...] [--timeout]`: the collector's verdict.

A waiter reads the front's job ledger, never the session log. The
collector already decides returned, failed, killed and the rest; this
verb prints one line once that line exists and exits with the worker's
code. Waiting on several ids prints one line each as they finish.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import caller, cli, paths, progress, store
from .caller import FOREMAN, MERGE_DESK, SUPERVISOR, Refusal
from .pools._common import timeout_seconds

#: How often a waiter re-reads the job ledger. Never the log.
POLL_SECONDS = 2.0

TERMINAL = frozenset({
    "returned", "returned-with-work", "failed", "killed", "died", "verified",
    "history",
})

OK_WITHOUT_CODE = frozenset({"returned", "returned-with-work", "verified"})
FAIL_WITHOUT_CODE = frozenset({"failed", "killed", "died"})


def _folded_job(front: str, job_id: str) -> dict | None:
    """The latest folded line for ``job_id`` on ``front``, or None."""
    try:
        records = store.read_ledger(paths.front_jobs_path(front))
    except OSError:
        return None
    latest: dict | None = None
    for record in store.fold_by_id(records):
        if record.get("id") == job_id:
            latest = record
    return latest


def _ledger_index(front: str, job_id: str) -> int:
    """Index of the latest line for ``job_id`` on ``front``, or -1."""
    try:
        records = store.read_ledger(paths.front_jobs_path(front))
    except OSError:
        return -1
    index = -1
    for i, record in enumerate(records):
        if record.get("id") == job_id:
            index = i
    return index


def _resolve_id(token: str) -> tuple[str | None, dict | None]:
    """The front and folded job for a job id or a session id."""
    front, record = progress._find_job(token)
    if record is not None:
        return front, record
    sessions = caller.read_roster().get("sessions", {})
    session = sessions.get(token) if isinstance(sessions, dict) else None
    if not isinstance(session, dict):
        return None, None
    front = session.get("front")
    job_id = session.get("job")
    if not isinstance(front, str) or not front:
        return None, None
    if not isinstance(job_id, str) or not job_id:
        return None, None
    record = _folded_job(front, job_id)
    if record is None:
        return None, None
    return front, record


def _outcome(record: dict) -> str:
    state = record.get("state") or ""
    if state == "failed" and record.get("outcome_reason") == "job timed out":
        return "timed out"
    return str(state)


def _exit_status(record: dict) -> int:
    code = record.get("exit_code")
    if isinstance(code, bool) or not isinstance(code, int):
        state = record.get("state")
        if state in OK_WITHOUT_CODE:
            return 0
        if state in FAIL_WITHOUT_CODE:
            return 1
        return 0
    return code


def _format_line(record: dict) -> str:
    job_id = record.get("id") or "?"
    code = record.get("exit_code")
    exit_s = (
        str(code) if isinstance(code, int) and not isinstance(code, bool)
        else "?"
    )
    branch = record.get("branch") if isinstance(record.get("branch"), str) else ""
    head = record.get("head")
    head_s = head if isinstance(head, str) and head else "?"
    log = record.get("log") if isinstance(record.get("log"), str) else ""
    return (
        f"{job_id} {_outcome(record)} exit={exit_s} "
        f"branch={branch} head={head_s} log={log}"
    )


def wait_main(ids: list[str], timeout: str | None = None) -> int:
    """Block until each id has a terminal job line, then print and exit."""
    verb = "wait"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, SUPERVISOR, FOREMAN, MERGE_DESK,
                      violations=violations)
    tokens = [token.strip() for token in ids if isinstance(token, str)]
    if not tokens:
        violations.append("field 'id' is required")
    seconds: int | None = None
    if timeout is not None:
        seconds = timeout_seconds(timeout)
        if seconds is None:
            violations.append(
                f"bad timeout {timeout!r}; use a number with a unit, e.g. 20m"
            )
    pending: list[tuple[str, str]] = []
    seen: set[str] = set()
    for token in tokens:
        if not token or token in seen:
            continue
        seen.add(token)
        front, record = _resolve_id(token)
        if record is None or not front:
            violations.append(f"unknown job or session '{token}'")
            continue
        job_id = record.get("id")
        if not isinstance(job_id, str) or not job_id:
            violations.append(f"unknown job or session '{token}'")
            continue
        pending.append((front, job_id))
    if violations:
        return Refusal(violations).report()

    deadline = None if seconds is None else time.monotonic() + seconds
    remaining = list(pending)
    codes: list[int] = []
    while remaining:
        finished: list[tuple[int, dict, tuple[str, str]]] = []
        still: list[tuple[str, str]] = []
        for item in remaining:
            front, job_id = item
            record = _folded_job(front, job_id)
            if record is not None and record.get("state") in TERMINAL:
                finished.append((_ledger_index(front, job_id), record, item))
            else:
                still.append(item)
        finished.sort(key=lambda row: row[0])
        for _index, record, _item in finished:
            print(_format_line(record), flush=True)
            codes.append(_exit_status(record))
        remaining = still
        if not remaining:
            break
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            for _front, job_id in remaining:
                print(f"{job_id} waiting …", file=sys.stderr, flush=True)
            return 124
        sleep_for = POLL_SECONDS
        if deadline is not None:
            sleep_for = min(sleep_for, max(0.0, deadline - now))
        time.sleep(sleep_for)
    return max(codes) if codes else 0


def add_wait_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "ids", nargs="+", metavar="id",
        help="job id or session id to wait on",
    )
    sub.add_argument(
        "--timeout", default=None,
        help="how long to wait (e.g. 20m); omit to wait without limit",
    )


@cli.subcommand(
    "wait",
    help="Wait for a job's outcome on the ledger, never the log.",
    description=(
        "Block until the collector has recorded each job's outcome on "
        "the front's job ledger. A waiter never opens the session log."
    ),
)
def _wait_entry(args: argparse.Namespace) -> int:
    return wait_main(args.ids, args.timeout)


_wait_entry.add_arguments = add_wait_arguments  # type: ignore[attr-defined]
