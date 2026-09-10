"""`foreman status`: the one screen, as plain text.

The same text the panel will render later, in the docs/DESIGN.md section 13
order and no other: header, Needs you, Problems, Working, Done, the job
queue, the merge queue, Capacity, and the Overall line last. Status reads; it
never writes, and it never
recomputes a number the collector already derived: per-job elapsed/timeout/
write-idle, per-session seconds since declared write/activity, pool
held/total and the swarm counts all come from ``observed.json``. Names,
questions, details and checkpoints come straight from the ledgers through
``paths.py`` — nothing here builds a path or opens a state file by hand.

Two version notes. The front queue (front admission) has no writer in
v0, so that block is left out entirely rather than printed empty. Every
other block with nothing in it prints one line saying so, never an empty
heading. Monitors (``measurements.jsonl``) render under each front in
Working, one line per declared monitor plus the two free ones.

``foreman status --fixture <dir>`` reads a state directory from that path
instead of the real one. ``FOREMAN_NOW`` (an ISO timestamp) pins the clock
so the golden test's ages are stable; without it the clock is now.

The gathering is public and shared: :func:`front_names`, :func:`front_tasks`,
:func:`front_jobs`, :func:`front_record_of`, :func:`front_measurements`,
:func:`open_anomalies`, :func:`merge_rows` and :func:`session_checkpoint`
return structured data, and :mod:`foreman.panel_feed` folds the panel's
summary out of the same calls this renderer makes. Two views, one read of
the state directory, so they cannot drift onto different bytes.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import caller, paths, store
from .caller import FOREMAN, SUPERVISOR, Refusal
from .cli import subcommand
from .verbs import read_inbox

#: Queued work, in the supervisor's order: ledger order, first appearance.
QUEUE_STATES = ("planned", "queued")
TERMINAL = ("returned", "returned-with-work", "verified", "failed", "killed",
            "history")

#: Job states that finished work, the pace behind a front's ESTIMATE.
#: Failed and killed jobs ended work without moving it, so they carry no
#: rate. A job that returned with work moved its branch, so it counts.
COMPLETED_JOB_STATES = ("returned", "returned-with-work", "verified")

#: Hours of job completions behind every ESTIMATE rate (DESIGN section 7:
#: landed units per hour over the last N hours).
ESTIMATE_WINDOW_H = 24

#: The terminal is narrow: no status line wider than this that a shorter
#: wording or a truncation can avoid.
LINE_WIDTH = 100


def _fit(line: str) -> str:
    """Clip one status line to the readable width, never wrapping."""
    if len(line) <= LINE_WIDTH:
        return line
    return line[:LINE_WIDTH - 3] + "..."


def _wrapped(label: str, items: list[str], indent: str = "    ") -> list[str]:
    """``label`` and its items over as many lines as they need.

    A list clipped at the width loses exactly the items at its end, and the
    remaining tasks are the point of the line. Nothing is dropped: the list
    continues on an indented line instead.
    """
    lines: list[str] = []
    current = f"{indent}{label}"
    lead = indent + " " * 4
    for index, item in enumerate(items):
        piece = item if index == len(items) - 1 else f"{item},"
        if len(current) + 1 + len(piece) > LINE_WIDTH and \
                current.strip() not in (label.strip(),):
            lines.append(current)
            current = f"{lead}{piece}"
        else:
            current = f"{current} {piece}"
    lines.append(current)
    return lines


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _one_line(text: object) -> str:
    """One line of prose: collapse every run of whitespace to one space."""
    return " ".join(str(text or "").split())

NOW_ENV = "FOREMAN_NOW"


def _now() -> datetime:
    raw = os.environ.get(NOW_ENV)
    if raw:
        moment = _parse_time(raw)
        if moment is not None:
            return moment
    return datetime.now(timezone.utc)


def _parse_time(text: str | None) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _age(seconds: float | None) -> str:
    """Human age as the owner reads it: 14m, 2h. Never a timestamp."""
    if seconds is None:
        return "?"
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h"
    return f"{total // 86400}d"


def _since(text: str | None, now: datetime) -> str:
    moment = _parse_time(text)
    if moment is None:
        return "?"
    return _age((now - moment).total_seconds())


#: The ledger fold every reader shares; see :func:`foreman.store.fold_by_id`.
_fold_by_id = store.fold_by_id


def open_anomalies(records: list[dict]) -> list[dict]:
    """One open line per (kind, subject): a resolution appends a revised
    copy, so fold last-wins exactly like the collector does."""
    order: list[tuple[str, str]] = []
    folded: dict[tuple[str, str], dict] = {}
    for record in records:
        kind, subject = record.get("kind"), record.get("subject")
        if not (isinstance(kind, str) and isinstance(subject, str)):
            continue
        key = (kind, subject)
        if key not in folded:
            order.append(key)
        folded[key] = record
    return [folded[key] for key in order
            if folded[key].get("resolved_at") is None]


def front_names() -> list[str]:
    try:
        return sorted(path.name for path in paths.fronts_dir().iterdir()
                      if path.is_dir())
    except OSError:
        return []


def front_tasks(name: str) -> list[dict]:
    try:
        return _fold_by_id(store.read_ledger(paths.front_tasks_path(name)))
    except OSError:
        return []


def front_jobs(name: str) -> list[dict]:
    try:
        return _fold_by_id(store.read_ledger(paths.front_jobs_path(name)))
    except OSError:
        return []


def front_record_of(name: str) -> dict | None:
    """The folded front line, or None for a front that predates `front add`.

    A front with no record renders exactly as it always has: the caller adds
    nothing, so the v0 golden file holds byte for byte.
    """
    try:
        folded = _fold_by_id(store.read_ledger(paths.front_record_path(name)))
    except OSError:
        return None
    return folded[-1] if folded else None


def _slots_held() -> dict[tuple[str, str], int]:
    """Open slot grants per (front, role), folded last-wins like every reader.

    A grant counts while its `released_at` is null; a missing slot ledger
    holds nothing.
    """
    try:
        records = store.read_ledger(paths.slots_path())
    except OSError:
        return {}
    held: dict[tuple[str, str], int] = {}
    for grant in _fold_by_id(records):
        front, role = grant.get("front"), grant.get("role")
        if grant.get("released_at") is None and \
                isinstance(front, str) and front and \
                isinstance(role, str) and role:
            held[(front, role)] = held.get((front, role), 0) + 1
    return held


def merge_rows() -> list[dict] | None:
    """None when the ledger was never written; [] when written but empty."""
    try:
        return _fold_by_id(store.read_ledger(paths.merges_path()))
    except OSError:
        return None


def session_checkpoint(session_id: str) -> dict | None:
    snapshot = store.read_snapshot(paths.checkpoint_path(session_id),
                                   default=None)
    return snapshot if isinstance(snapshot, dict) else None


def _headless_tail(sid: str, record: dict, now: datetime) -> str:
    """What a headless supervisor is doing, from its two ledgers.

    The last wake reason, the turn count, the age of the last turn, and
    `turn running` while one's marker is fresh — never from a process, so
    a headless session with no pid reads as normal, not as dead. Empty
    for every session that is not headless, so the windowed lines read
    exactly as they always have.
    """
    if not record.get("headless"):
        return ""
    from . import headless as _headless
    from . import wake as _wake

    parts = []
    last = _wake.last_wake(sid)
    if last is None:
        parts.append("no wake yet")
    else:
        reason = last.get("reason") or "?"
        age = _since(last.get("at"), now)
        when = f"{age} ago" if age != "?" else "at an unknown time"
        parts.append(f"last wake '{reason}' {when}")
    try:
        turns = _headless.read_turns(sid)
    except OSError:
        turns = []
    if turns:
        age = _since(turns[-1].get("at"), now)
        when = f"{age} ago" if age != "?" else "at an unknown time"
        parts.append(f"{_count(len(turns), 'turn')} · last turn {when}")
    else:
        parts.append("0 turns")
    if _wake.turn_fresh(sid, now=now):
        parts.append("turn running")
    return " · " + " · ".join(parts)


def _is_seeded(checkpoint: dict | None) -> bool:
    return isinstance(checkpoint, dict) and checkpoint.get("seeded") is True


def _checkpoint_age(checkpoint: dict | None, record: dict,
                    sessions_view: dict, sid: str, now: datetime) -> str:
    """Age of a seeded checkpoint: the file's `at`, then the collector,
    then the roster stamp."""
    at = (checkpoint or {}).get("at") if isinstance(checkpoint, dict) else None
    if isinstance(at, str) and at.strip():
        age = _since(at, now)
        if age != "?":
            return age
    observed_age = sessions_view.get(sid, {}).get("seconds_since_declared")
    if isinstance(observed_age, (int, float)):
        return _age(observed_age)
    return _since(record.get("last_declared_at"), now)


def _doing_line(sid: str, record: dict, sessions_view: dict,
                now: datetime) -> str | None:
    """The supervisor's doing-now with its age. The age is the collector's
    seconds-since-declared where it has one; the roster stamp otherwise."""
    checkpoint = session_checkpoint(sid)
    doing = (checkpoint or {}).get("doing")
    if not (isinstance(doing, str) and doing.strip()):
        return None
    observed_age = sessions_view.get(sid, {}).get("seconds_since_declared")
    if isinstance(observed_age, (int, float)):
        age = _age(observed_age)
    else:
        age = _since(record.get("last_declared_at"), now)
    return f"{doing.strip()} ({age} ago)"


def _doing_suffix(sid: str, record: dict, sessions_view: dict,
                  now: datetime) -> str:
    """The working-line tail: `doing now`, or the seeded orientation."""
    checkpoint = session_checkpoint(sid)
    if _is_seeded(checkpoint):
        age = _checkpoint_age(checkpoint, record, sessions_view, sid, now)
        return f" \u00b7 orienting (seeded {age} ago)"
    doing = _doing_line(sid, record, sessions_view, now)
    return f" \u00b7 doing now: {doing}" if doing else ""


def _supervisor_for(front: str, roster: dict) -> tuple[str, dict] | None:
    """The front's live supervisor: the front record names it first.

    `launch supervisor` and `front take` write the session onto the front
    record; a session it names that is still starting or running wins over
    roster order, so a replaced predecessor never shadows the live
    successor that is actually checkpointing. Anything else falls back to
    the roster scan below.
    """
    record = front_record_of(front)
    named = record.get("supervisor") if isinstance(record, dict) else None
    if isinstance(named, str) and named:
        entry = roster.get(named)
        if isinstance(entry, dict) and entry.get("role") == "supervisor" \
                and entry.get("front") == front \
                and entry.get("state") in ("starting", "running"):
            return named, entry
    running = backup = None
    for sid, record in roster.items():
        if not isinstance(record, dict):
            continue
        if record.get("role") != "supervisor":
            continue
        if record.get("front") != front:
            continue
        if record.get("state") == "running" and running is None:
            running = (sid, record)
        if backup is None:
            backup = (sid, record)
    return running or backup


def _header(roster: dict, observed: dict | None, now: datetime) -> str:
    if isinstance(observed, dict) and isinstance(
            observed.get("swarm"), dict):
        swarm = observed["swarm"]
        registered = swarm.get("sessions_registered", len(roster))
        seen = swarm.get("sessions_observed", 0)
    else:
        registered, seen = len(roster), 0
    frozen = "frozen" if paths.frozen_path().exists() else "live"
    if isinstance(observed, dict) and _parse_time(observed.get("at")):
        tick = f"collector {_since(observed.get('at'), now)} ago"
    else:
        tick = "collector never ticked"
    try:
        stale = any(record.get("kind") == "collector stale"
                    for record in open_anomalies(
                        store.read_ledger(paths.anomalies_path())))
    except OSError:
        stale = False
    if stale:
        tick += " \u00b7 collector stale \u2014 foreman collector restart"
    header = (f"Foreman status \u2014 {registered} sessions registered, "
              f"{seen} observed \u00b7 {frozen} \u00b7 {tick}")
    # A v5 swarm is held by one foreman `foreman start` brought up; the
    # header names it. Older swarms render exactly as they always have.
    if any((front_record_of(name) or {}).get("shape") == "v5"
           for name in front_names()):
        from .launch import foreman_summary

        header += f"\nforeman: {foreman_summary() or 'none'}"
    return header


def _open_inbox() -> list[dict]:
    """Every inbox item still waiting on the owner, oldest first."""
    items = [item for item in read_inbox()[0]
             if item.get("answered_at") is None]
    items.sort(key=lambda item: item.get("asked_at") or "")
    return items


def _blocked_for(name: str, roster: dict,
                 inbox: list[dict]) -> list[dict]:
    """The open inbox items asked by this front's own sessions.

    An item belongs to the front its asking session watches; the asker's
    roster line says which front that is. Items from sessions the roster
    no longer names belong to no front and stay only in Needs you.
    """
    blocked = []
    for item in inbox:
        for key in ("from", "by"):
            record = roster.get(item.get(key) or "")
            if isinstance(record, dict) and record.get("front") == name:
                blocked.append(item)
                break
    return blocked


def _estimate_for(tasks: list[dict], jobs: list[dict],
                  now: datetime) -> tuple[int, int, int, float | None]:
    """(tasks remaining, tasks total, jobs finished in the window,
    projected hours). Hours is None where no job finished lately: no
    pace, no projection — the screen says "no rate yet" instead."""
    remaining = sum(1 for task in tasks if task.get("state") != "landed")
    cutoff = now - timedelta(hours=ESTIMATE_WINDOW_H)
    finished = 0
    for job in jobs:
        if job.get("state") not in COMPLETED_JOB_STATES:
            continue
        moment = _parse_time(job.get("verified_at")
                             or job.get("returned_at"))
        if moment is None:
            continue
        if cutoff <= moment <= now:
            finished += 1
    hours = (remaining / (finished / ESTIMATE_WINDOW_H)
             if finished else None)
    return remaining, len(tasks), finished, hours


def _format_hours(hours: float) -> str:
    minutes = max(1, int(round(hours * 60)))
    if minutes < 60:
        return f"about {minutes}m"
    if minutes // 60 < 48:
        return f"about {minutes // 60}h"
    return f"about {minutes // 60 // 24}d"


def _needs_you(now: datetime) -> list[str]:
    items = _open_inbox()
    if not items:
        return ["Needs you: nothing needs you."]
    lines = [f"Needs you ({len(items)}):"]
    for item in items:
        options = item.get("options") or []
        if options:
            how = f"foreman answer {item.get('id')} <{'|'.join(options)}>"
        else:
            how = f"foreman answer {item.get('id')} <text>"
        lines.append(
            f"  [{item.get('kind')} \u00b7 "
            f"{_since(item.get('asked_at'), now)}] "
            f"{item.get('question')} \u2014 recommend: "
            f"{item.get('recommendation')} ({how})")
    return lines


def _problem_context(kind: str, subject: str, roster: dict,
                     jobs_by_session: dict) -> str | None:
    """What the anomaly is about, named by what it does: the task title a
    stalled job works on, or the front name a supervisor watches."""
    record = roster.get(subject)
    if not isinstance(record, dict):
        return None
    if kind.startswith("job"):
        job = jobs_by_session.get(subject)
        if job is not None:
            return job
        return record.get("front")
    if kind.startswith("supervisor"):
        return record.get("front")
    return None


def _problems(roster: dict, jobs_by_session: dict, now: datetime) -> list[str]:
    try:
        records = store.read_ledger(paths.anomalies_path())
    except OSError:
        records = []
    open_lines = open_anomalies(records)
    if not open_lines:
        return ["Problems: none."]
    lines = [f"Problems ({len(open_lines)}):"]
    for record in open_lines:
        kind, subject = record.get("kind"), record.get("subject")
        context = _problem_context(kind, subject, roster, jobs_by_session)
        detail = record.get("detail") or ""
        head = f"{kind}: {context} \u2014 {detail}" if context \
            else f"{kind}: {detail}"
        lines.append(f"  - {head} ({subject}, "
                     f"{_since(record.get('since'), now)})")
    return lines


def _job_detail(job: dict, observed_jobs: dict, roster: dict,
                now: datetime) -> str:
    """One job under its task. Elapsed, timeout and write-idle are the
    collector's numbers from observed.json; the stalled word is the
    collector's roster state."""
    role, state = job.get("role") or "?", job.get("state") or "?"
    seen = observed_jobs.get(job.get("id") or "")
    seen = seen if isinstance(seen, dict) else {}
    session = roster.get(job.get("session") or "")
    session = session if isinstance(session, dict) else {}
    if session.get("state") == "stalled":
        state = "stalled"
    if state in QUEUE_STATES:
        stamp = job.get("queued_at") or job.get("planned_at")
        return f"{role} job queued (waiting {_since(stamp, now)})"
    if state in TERMINAL:
        stamp = job.get("verified_at") or job.get("returned_at")
        detail = f"{role} job {state} {_since(stamp, now)} ago"
        branch = job.get("branch")
        if isinstance(branch, str) and branch.strip():
            detail += f" \u00b7 {branch.strip()}"
            head = job.get("head")
            if isinstance(head, str) and head.strip():
                detail += f" @ {head.strip()[:7]}"
        because = job.get("verify_because")
        if isinstance(because, str) and because.strip():
            detail += f" \u2014 {' '.join(because.split())}"
        return detail
    elapsed = _age(seen.get("elapsed_s"))
    timeout = _age(seen.get("timeout_s"))
    idle = seen.get("minutes_since_write")
    idle_text = f", write {_age(idle * 60)} ago" \
        if isinstance(idle, (int, float)) else ""
    return f"{role} job {state} {elapsed} of {timeout}{idle_text}"


def _task_line(task: dict, titles: dict[str, str]) -> str:
    after = [titles.get(name, name) for name in (task.get("after") or [])]
    head = (f"{task.get('title')} \u2014 {task.get('state')} "
            f"\u00b7 units {task.get('units_done', 0)}/"
            f"{task.get('units_total', 0)}")
    if after:
        head += f" \u00b7 after: {', '.join(after)}"
    if task.get("head"):
        head += f" \u00b7 head {task.get('head')}"
    if task.get("added_by"):
        head += f" \u00b7 added by {task.get('added_by')}"
    # A task that was moved backwards says so until it moves again: the
    # reset is the owner's business, not a quiet correction.
    if task.get("built_by_hand"):
        head += f" \u00b7 by hand: {task.get('built_by_hand')}"
    if task.get("reset_reason"):
        head += f" \u00b7 reset: {task.get('reset_reason')}"
    return head


def front_measurements(name: str) -> list[dict]:
    try:
        return store.read_ledger(paths.front_measurements_path(name))
    except OSError:
        return []


def _declared_monitors(front_record: dict | None) -> list[dict]:
    if not isinstance(front_record, dict):
        return []
    declared = front_record.get("monitors")
    if not isinstance(declared, list):
        return []
    return [entry for entry in declared if isinstance(entry, dict)]


def _doing_monitor_line(name: str, roster: dict, sessions_view: dict,
                        now: datetime) -> str:
    """The free `doing now` monitor, in the declared-monitor shape."""
    held = _supervisor_for(name, roster)
    if held is None:
        return "    doing now \u2014 no supervisor"
    sid, record = held
    checkpoint = session_checkpoint(sid)
    if _is_seeded(checkpoint):
        age = _checkpoint_age(checkpoint, record, sessions_view, sid, now)
        return f"    orienting (seeded {age} ago)"
    doing = (checkpoint or {}).get("doing")
    if not (isinstance(doing, str) and doing.strip()):
        return "    doing now \u2014 no checkpoint yet"
    observed_age = sessions_view.get(sid, {}).get("seconds_since_declared")
    if isinstance(observed_age, (int, float)):
        age = _age(observed_age)
    else:
        age = _since(record.get("last_declared_at"), now)
    return f"    doing now \u2014 {doing.strip()} \u00b7 {age} ago"


def _progress_monitor_line(tasks: list[dict]) -> str:
    """The free progress monitor, in the declared-monitor shape."""
    if not tasks:
        return "    progress \u2014 no tasks yet"
    landed = sum(1 for task in tasks if task.get("state") == "landed")
    return f"    progress \u2014 {landed}/{len(tasks)} tasks"


def _monitor_lines(name: str, front_record: dict | None,
                   tasks: list[dict], roster: dict,
                   sessions_view: dict, now: datetime) -> list[str]:
    """Declared monitors plus the two free ones, one line each.

    A declared line reads ``<question> \u2014 <value>/<of> \u00b7 <age> ago
    \u00b7 <trend>``, with the trailing trend omitted when only one
    measurement exists and the denominator omitted when none is known.
    The free monitors render in the same shape from what the runtime
    already knows, so a front with no declared monitors still shows two
    lines.
    """
    from . import monitors as _monitors

    lines = []
    ledger = front_measurements(name)
    for decl in _declared_monitors(front_record):
        question = decl.get("question") or decl.get("measure") or "?"
        question = str(question).strip() or "?"
        matched = _monitors.matching_measurements(ledger, decl)
        if not matched:
            lines.append(f"    {question} \u2014 no measurements yet")
            continue
        latest = matched[-1]
        value = latest.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            lines.append(f"    {question} \u2014 no measurements yet")
            continue
        denominator = latest.get("of")
        if denominator is None:
            denominator = decl.get("of")
        if isinstance(denominator, bool) or not isinstance(
                denominator, (int, float)):
            denominator = None
        if denominator is None:
            reading = _monitors.format_number(value)
        else:
            reading = (f"{_monitors.format_number(value)}/"
                       f"{_monitors.format_number(denominator)}")
        unit = decl.get("unit")
        if isinstance(unit, str) and unit.strip():
            reading += f" {unit.strip()}"
        age = _since(latest.get("at"), now)
        previous = matched[-2] if len(matched) > 1 else None
        prev_value = previous.get("value") if isinstance(
            previous, dict) else None
        if isinstance(prev_value, bool) or not isinstance(
                prev_value, (int, float)):
            prev_value = None
        trend = _monitors.trend_of(
            float(prev_value) if prev_value is not None else None,
            float(value))
        if trend:
            lines.append(f"    {question} \u2014 {reading} "
                         f"\u00b7 {age} ago \u00b7 {trend}")
        else:
            lines.append(f"    {question} \u2014 {reading} \u00b7 {age} ago")
    lines.append(_doing_monitor_line(name, roster, sessions_view, now))
    lines.append(_progress_monitor_line(tasks))
    return lines


def _load_fronts() -> tuple[list[tuple[str, list[dict], list[dict]]],
                          dict[str, str], dict[str, str]]:
    """Every front with its folded tasks and jobs, plus the global
    task-title and session->task-name maps both Working and Problems read."""
    loaded: list[tuple[str, list[dict], list[dict]]] = []
    titles: dict[str, str] = {}
    jobs_by_session: dict[str, str] = {}
    for name in front_names():
        tasks, jobs = front_tasks(name), front_jobs(name)
        local = {task.get("id"): task.get("title") for task in tasks
                 if task.get("id")}
        titles.update(local)
        # A job may name its task by title, which is what `--task` accepts.
        # The launcher resolves it now, but records written before that do
        # not match any task id, so every one of them was drawn a second
        # time under "the task no ledger names". Resolve on the way in and
        # the screen has one rule: a job hangs under its task.
        by_title = {title: tid for tid, title in local.items()
                    if isinstance(title, str)}
        jobs = [dict(job, task=by_title[job["task"]])
                if isinstance(job.get("task"), str)
                and job["task"] in by_title else job
                for job in jobs]
        for job in jobs:
            if isinstance(job.get("session"), str) and \
                    isinstance(job.get("task"), str):
                jobs_by_session[job["session"]] = local.get(
                    job["task"], job["task"])
        loaded.append((name, tasks, jobs))
    return loaded, titles, jobs_by_session


def _front_tail_lines(name: str, front_record: dict | None,
                      tasks: list[dict], jobs: list[dict],
                      roster: dict, inbox: list[dict],
                      now: datetime) -> list[str]:
    """The four lines every front block ends with, never omitted.

    What the front is (its want, one line), what is left (the tasks not
    landed, by title), when it finishes (projected from the jobs lately
    finished, with that basis on the same line, or "no rate yet"), and
    what waits on the owner. A field with no data says so in words: a
    missing line and a line saying nothing is happening are different
    facts and the owner cannot tell them apart.
    """
    want = (front_record or {}).get("want")
    want = _one_line(want) if isinstance(want, str) else ""
    lines = [_fit(f"    want: {want}" if want else
                  "    want: (not recorded)")]
    left = [task.get("title") for task in tasks
            if task.get("state") != "landed" and task.get("title")]
    if not tasks:
        lines.append("    REMAINING: no tasks yet")
    elif not left:
        lines.append(_fit(f"    REMAINING: none \u2014 all "
                          f"{_count(len(tasks), 'task')} landed"))
    else:
        # Never clipped: a remaining task the owner cannot see is a task he
        # does not know is left.
        lines.extend(_wrapped(f"REMAINING ({len(left)}):",
                              [str(title) for title in left]))
    remaining, total, finished, hours = _estimate_for(tasks, jobs, now)
    if not tasks:
        lines.append("    ESTIMATE: no rate yet \u2014 no tasks yet")
    elif not remaining:
        lines.append(_fit(f"    ESTIMATE: done \u2014 all "
                          f"{_count(total, 'task')} landed"))
    elif hours is None:
        lines.append(_fit(f"    ESTIMATE: no rate yet \u2014 "
                          f"{_count(remaining, 'task')} remaining"))
    else:
        lines.append(_fit(f"    ESTIMATE: {_format_hours(hours)} "
                          f"({_count(finished, 'job')} in last "
                          f"{ESTIMATE_WINDOW_H}h)"))
    blocked = _blocked_for(name, roster, inbox)
    if not blocked:
        lines.append("    Blocked on you: nothing blocked on you")
    else:
        parts = [f"{_one_line(item.get('question')) or '(no question)'} "
                 f"({item.get('kind') or '?'}"
                 f", {_since(item.get('asked_at'), now)})"
                 for item in blocked]
        lines.append(_fit(f"    Blocked on you ({len(blocked)}): "
                          f"{'; '.join(parts)}"))
    return lines


def _merge_head(tasks: list[dict]) -> str | None:
    """The head the front merged at: its latest landing's head.

    A front closed with `--merged` lands every built task at one sha, so
    this is that sha; a front landed task by task reports its last landing.
    None where no landed task recorded a head.
    """
    best: dict | None = None
    for task in tasks:
        if task.get("state") != "landed" or not task.get("head"):
            continue
        if best is None or (task.get("landed_at") or "") >= \
                (best.get("landed_at") or ""):
            best = task
    head = (best or {}).get("head")
    return head if isinstance(head, str) and head else None


def _is_done(name: str) -> bool:
    """True when the front's folded record says it ran to completion."""
    return (front_record_of(name) or {}).get("state") == "done"


def _done_block(loaded: list[tuple[str, list[dict], list[dict]]],
                now: datetime) -> list[str]:
    """Finished fronts, one line each, and nothing else about them.

    A front at state `done` prints once — name, landed n/n, merge sha,
    when — and appears in no other block. With no done front there is no
    block at all, so the empty screen reads exactly as it always has.
    """
    rows = [(name, tasks) for name, tasks, _j in loaded
            if _is_done(name)]
    if not rows:
        return []
    lines = ["Done:"]
    for name, tasks in rows:
        record = front_record_of(name) or {}
        landed = sum(1 for task in tasks if task.get("state") == "landed")
        head = _merge_head(tasks) or "(no head recorded)"
        when = _since(record.get("at"), now)
        closed = f"done {when} ago" if when != "?" \
            else "done (not recorded)"
        label = name
        if record.get("fixture"):
            label = f"{name} (fixture)"
        lines.append(_fit(f"  {label} \u2014 {landed}/{len(tasks)} landed "
                          f"\u00b7 merge {head} \u00b7 {closed}"))
    return lines


def _finding_line(finding: dict) -> str:
    """One finding on the Working block: id with the title, or the title
    alone when the ledger line predates ids."""
    title = _one_line(finding.get("title") or "")
    fid = finding.get("id")
    if isinstance(fid, str) and fid.strip():
        if title:
            return f"      {fid.strip()} {title}"
        return f"      {fid.strip()}"
    return f"      {title}"


def _front_base_sha(front_record: dict | None) -> str:
    """The checkout a v5 front contracted, or empty when none is recorded.

    A top-level ``base_sha`` wins; otherwise the first repository that
    recorded one. Old fronts carry neither and never mark evidence.
    """
    if not isinstance(front_record, dict):
        return ""
    sha = front_record.get("base_sha")
    if isinstance(sha, str) and sha:
        return sha
    repos = front_record.get("repositories")
    if not isinstance(repos, list):
        return ""
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        sha = repo.get("base_sha")
        if isinstance(sha, str) and sha:
            return sha
    return ""


def _evidence_line(record: dict, base_sha: str) -> str:
    """One evidence line, with the other-build marker when it does not
    match the front's contracted checkout."""
    claim = _one_line(record.get("claim") or "")
    marker = store.other_build_marker(record, base_sha)
    return f"      {claim}{marker}"


def _v5_queue_line(name: str, front_record: dict | None) -> str | None:
    """``queue: N waiting, M running`` for a v5 front's tree, else None."""
    if (front_record or {}).get("shape") != "v5":
        return None
    try:
        tree = _fold_by_id(store.read_ledger(paths.front_tree_path(name)))
    except OSError:
        tree = []
    waiting = sum(1 for node in tree
                  if node.get("kind") == "job"
                  and str(node.get("state") or "") == "queued")
    running = sum(1 for node in tree
                  if node.get("kind") == "job"
                  and str(node.get("state") or "") == "running")
    return f"    queue: {waiting} waiting, {running} running"


def _working(roster: dict, observed: dict | None, now: datetime,
             loaded: list[tuple[str, list[dict], list[dict]]],
             inbox: list[dict]) -> list[str]:
    loaded = [(name, tasks, jobs) for name, tasks, jobs in loaded
              if not _is_done(name)]
    if not loaded:
        return ["Working: nothing running."]
    sessions_view = (observed or {}).get("sessions") or {}
    observed_jobs = (observed or {}).get("jobs") or {}
    lines = ["Working:"]
    for name, tasks, jobs in loaded:
        titles = {task.get("id"): task.get("title") for task in tasks
                  if task.get("id")}
        landed = sum(1 for task in tasks if task.get("state") == "landed")
        built = sum(1 for task in tasks if task.get("state") == "built")
        progress = (f"tasks {landed} landed, {built} built, "
                    f"{len(tasks)} total")
        held = _supervisor_for(name, roster)
        front_record = front_record_of(name)
        # A front for trying the runtime out says so on every line it owns:
        # a task a probe job moved must never read as work a front did.
        label = name
        if (front_record or {}).get("fixture"):
            label = f"{name} (fixture)"
        if held is None:
            lines.append(f"  {label} \u2014 no supervisor \u00b7 {progress}")
        else:
            sid, record = held
            tail = _doing_suffix(sid, record, sessions_view, now)
            tail += _headless_tail(sid, record, now)
            lines.append(f"  {label} \u2014 supervisor attached "
                         f"\u00b7 {progress}{tail}")
        if front_record is not None:
            done_when = front_record.get("done_when") or ""
            if isinstance(done_when, str) and done_when.strip():
                lines.append(f"    {done_when.strip()}")
            allocation = front_record.get("allocation") or {}
            if isinstance(allocation, dict) and allocation:
                from . import capacity
                open_grants = _slots_held()
                parts = []
                for role in sorted(allocation):
                    shown = capacity.allocation_out(role, now)
                    if shown is None:
                        parts.append(
                            f"{role} {open_grants.get((name, role), 0)}/"
                            f"{allocation[role]}")
                    else:
                        parts.append(f"{role} {shown}")
                lines.append(f"    allocation: {', '.join(parts)}")
            queue_line = _v5_queue_line(name, front_record)
            if queue_line is not None:
                lines.append(queue_line)
        try:
            evidence = store.read_ledger(paths.front_evidence_path(name))
        except OSError:
            evidence = []
        try:
            findings = store.read_ledger(paths.front_findings_path(name))
        except OSError:
            findings = []
        if evidence or findings:
            confirmed = sum(1 for line in evidence
                            if line.get("status") == "CONFIRMED")
            base_sha = _front_base_sha(front_record)
            other = sum(1 for line in evidence
                        if store.other_build_marker(line, base_sha))
            confirmed_bit = f"{confirmed} confirmed"
            if other:
                confirmed_bit += f", {other} from another build"
            lines.append(f"    evidence: {len(evidence)} "
                         f"({confirmed_bit}) "
                         f"\u00b7 findings: {len(findings)}")
            for line in evidence:
                lines.append(_evidence_line(line, base_sha))
            for finding in findings:
                lines.append(_finding_line(finding))
        lines.extend(_monitor_lines(name, front_record, tasks, roster,
                                    sessions_view, now))
        for task in tasks:
            lines.append(f"    {_task_line(task, titles)}")
            for job in jobs:
                if job.get("task") != task.get("id"):
                    continue
                if job.get("state") in QUEUE_STATES:
                    continue
                detail = _job_detail(job, observed_jobs, roster, now)
                lines.append(f"      {detail}")
        # A job whose task no ledger names is still running on the
        # owner's machine, and a screen that hides it is worse than no
        # screen. In v0 nothing plans tasks yet, so this is every job.
        for job in jobs:
            if job.get("task") in titles or job.get("state") in QUEUE_STATES:
                continue
            detail = _job_detail(job, observed_jobs, roster, now)
            named = job.get("task") or "no task named"
            lines.append(f"    {named}")
            lines.append(f"      {detail}")
        lines.extend(_front_tail_lines(name, front_record, tasks, jobs,
                                       roster, inbox, now))
    return lines


def _overall(loaded: list[tuple[str, list[dict], list[dict]]],
             inbox: list[dict], now: datetime) -> str:
    """The whole swarm in one sentence: how many fronts are moving, the
    nearest finish, and what needs the owner."""
    done = sum(1 for name, _t, _j in loaded
               if (front_record_of(name) or {}).get("state") == "done")
    nearest: tuple[float, str] | None = None
    for name, tasks, jobs in loaded:
        if (front_record_of(name) or {}).get("state") == "done":
            continue
        remaining, _total, _finished, hours = _estimate_for(
            tasks, jobs, now)
        if hours is not None and remaining and \
                (nearest is None or hours < nearest[0]):
            nearest = (hours, name)
    if not loaded:
        head = "no fronts"
    else:
        head = (f"{len(loaded) - done} of {len(loaded)} fronts moving "
                f"({done} done)")
    if nearest is None:
        finish = "no projected finish yet"
    else:
        finish = f"nearest finish {_format_hours(nearest[0])} ({nearest[1]})"
    if not inbox:
        owner = "nothing needs you"
    else:
        owner = (f"{_count(len(inbox), 'item')}, "
                 f"oldest {_since(inbox[0].get('asked_at'), now)}")
    # The one line he reads first is never clipped. Clipping took the tail,
    # and the tail is what needs him — "needs you: nothing need..." says
    # less than nothing. The detail goes before the sentence does.
    line = f"Overall: {head}, {finish}; needs you: {owner}."
    if len(line) > LINE_WIDTH and inbox:
        line = (f"Overall: {head}, {finish}; needs you: "
                f"{_count(len(inbox), 'item')}.")
    if len(line) > LINE_WIDTH:
        line = f"Overall: {head}; needs you: {owner}."
    if len(line) > LINE_WIDTH:
        line = f"Overall: {head}; needs you: {_count(len(inbox), 'item')}." \
            if inbox else f"Overall: {head}; nothing needs you."
    return line


def _job_queue(now: datetime) -> list[str]:
    waiting: list[str] = []
    for name in front_names():
        if _is_done(name):
            continue
        tasks = {task.get("id"): task.get("title") for task in front_tasks(name)}
        for job in front_jobs(name):
            if job.get("state") not in QUEUE_STATES:
                continue
            what = tasks.get(job.get("task"), job.get("task") or "?")
            stamp = job.get("queued_at") or job.get("planned_at")
            waiting.append(f"  {name}: {what} waits for "
                           f"{job.get('role') or '?'} "
                           f"(waiting {_since(stamp, now)})")
    if not waiting:
        return ["Job queue: empty."]
    return ["Job queue:"] + waiting


def _merge_queue(titles: dict[str, str], now: datetime) -> list[str]:
    from .merge import is_failed, is_landed

    merges = merge_rows()
    if merges is None:
        return ["Merge queue: empty."]
    waiting = [row for row in merges
               if not is_landed(row) and not is_failed(row)]
    landed = [row for row in merges if is_landed(row)]
    failed = [row for row in merges if is_failed(row)]
    if not waiting and not landed and not failed:
        return ["Merge queue: empty."]
    lines = ["Merge queue:"]
    waiting.sort(key=lambda row: row.get("requested_at") or "")
    for row in waiting:
        tasks = ", ".join(titles.get(task, task)
                          for task in (row.get("tasks") or []))
        lines.append(f"  waiting: {row.get('front') or '(no front)'}: "
                     f"{row.get('branch')} -> "
                     f"{row.get('target')}, lands {tasks} "
                     f"(requested {_since(row.get('requested_at'), now)} "
                     f"ago)")
    landed.sort(key=lambda row: row.get("landed_at") or "")
    for row in landed:
        lines.append(f"  landed: {row.get('branch')} -> "
                     f"{row.get('target')} at {row.get('head') or '?'} "
                     f"({_since(row.get('landed_at'), now)} ago)")
    failed.sort(key=lambda row: row.get("failed_at") or "")
    for row in failed:
        lines.append(f"  failed: {row.get('branch')} -> "
                     f"{row.get('target')}: "
                     f"{row.get('fail_reason') or '(no reason recorded)'} "
                     f"({_since(row.get('failed_at'), now)} ago)")
    return lines


def _capacity(observed: dict | None, now: datetime | None = None) -> list[str]:
    """Held/cap per pool, held/ceiling per allocated front role, and the
    jobs waiting on a full pool — built from the slot ledger and the front
    records, so it is right whether or not the collector has ticked. The
    whole computation lives in :mod:`foreman.capacity`, imported here and
    not at the top so this module keeps the import list it had.

    ``now`` is the same clock Working uses, so a pool that is out cannot
    print held here and out there."""
    from . import capacity
    from . import resources as resources_mod

    lines = capacity.capacity_lines(observed, now=now)
    extra = resources_mod.status_lines()
    if not extra:
        return lines
    if lines == ["Capacity: no collector data yet."]:
        return ["Capacity:"] + extra
    return list(lines) + extra


def render(now: datetime | None = None) -> str:
    """The whole screen, in section 13 order. Pure text: no colour, no
    terminal control codes — a pipe gets exactly what a terminal gets."""
    moment = now or _now()
    roster = caller.read_roster().get("sessions", {})
    if not isinstance(roster, dict):
        roster = {}
    try:
        observed = store.read_snapshot(paths.observed_path(), default=None)
    except OSError:
        observed = None
    if not isinstance(observed, dict):
        observed = None
    loaded, titles, jobs_by_session = _load_fronts()
    inbox = _open_inbox()
    blocks = [_header(roster, observed, moment),
              *_needs_you(moment),
              *_problems(roster, jobs_by_session, moment),
              *_working(roster, observed, moment, loaded, inbox),
              *_done_block(loaded, moment),
              *_job_queue(moment),
              *_merge_queue(titles, moment),
              *_capacity(observed, moment),
              _overall(loaded, inbox, moment)]
    return "\n".join(blocks) + "\n"


def add_status_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fixture", default=None,
                        help="read a state directory from this path "
                             "instead of the real one")


@subcommand("status", help="Print the one-screen swarm status as text.")
def _status_entry(args: argparse.Namespace) -> int:
    return status_main(args.fixture)


_status_entry.add_arguments = add_status_arguments  # type: ignore[attr-defined]


def status_main(fixture: str | None = None) -> int:
    me, violations = caller.resolve("status")
    caller.check_role(me, "status", FOREMAN, SUPERVISOR,
                      violations=violations)
    if violations:
        return Refusal(violations).report()
    if fixture is not None:
        if not Path(fixture).is_dir():
            print(f"foreman status: no such fixture directory: {fixture}")
            return 1
        previous = os.environ.get(paths.STATE_ENV)
        os.environ[paths.STATE_ENV] = fixture
        try:
            print(render(), end="")
        finally:
            if previous is None:
                del os.environ[paths.STATE_ENV]
            else:
                os.environ[paths.STATE_ENV] = previous
        return 0
    print(render(), end="")
    return 0

