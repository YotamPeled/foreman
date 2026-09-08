"""`foreman status`: the one screen, as plain text.

The same text the panel will render later, in the docs/DESIGN.md section 13
order and no other: header, Needs you, Problems, Working, the job queue,
the merge queue, Capacity. Status reads; it never writes, and it never
recomputes a number the collector already derived: per-job elapsed/timeout/
write-idle, per-session seconds since declared write/activity, pool
held/total and the swarm counts all come from ``observed.json``. Names,
questions, details and checkpoints come straight from the ledgers through
``paths.py`` — nothing here builds a path or opens a state file by hand.

Two version notes. Monitors (``measurements.jsonl``) and the front
queue (front admission) have no writer in v0, so those blocks are left
out entirely rather than printed empty. Every other block with nothing in
it prints one line saying so, never an empty heading.

``foreman status --fixture <dir>`` reads a state directory from that path
instead of the real one. ``FOREMAN_NOW`` (an ISO timestamp) pins the clock
so the golden test's ages are stable; without it the clock is now.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from . import caller, paths, store
from .caller import FOREMAN, SUPERVISOR, Refusal
from .cli import subcommand
from .verbs import read_inbox

#: Queued work, in the supervisor's order: ledger order, first appearance.
QUEUE_STATES = ("planned", "queued")
TERMINAL = ("returned", "verified", "failed", "killed")

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


def _open_anomalies(records: list[dict]) -> list[dict]:
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


def _fronts() -> list[str]:
    try:
        return sorted(path.name for path in paths.fronts_dir().iterdir()
                      if path.is_dir())
    except OSError:
        return []


def _tasks(name: str) -> list[dict]:
    try:
        return _fold_by_id(store.read_ledger(paths.front_tasks_path(name)))
    except OSError:
        return []


def _jobs(name: str) -> list[dict]:
    try:
        return _fold_by_id(store.read_ledger(paths.front_jobs_path(name)))
    except OSError:
        return []


def _front_record(name: str) -> dict | None:
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


def _merges() -> list[dict] | None:
    """None when the ledger was never written; [] when written but empty."""
    try:
        return _fold_by_id(store.read_ledger(paths.merges_path()))
    except OSError:
        return None


def _checkpoint(session_id: str) -> dict | None:
    snapshot = store.read_snapshot(paths.checkpoint_path(session_id),
                                   default=None)
    return snapshot if isinstance(snapshot, dict) else None


def _doing_line(sid: str, record: dict, sessions_view: dict,
                now: datetime) -> str | None:
    """The supervisor's doing-now with its age. The age is the collector's
    seconds-since-declared where it has one; the roster stamp otherwise."""
    checkpoint = _checkpoint(sid)
    doing = (checkpoint or {}).get("doing")
    if not (isinstance(doing, str) and doing.strip()):
        return None
    observed_age = sessions_view.get(sid, {}).get("seconds_since_declared")
    if isinstance(observed_age, (int, float)):
        age = _age(observed_age)
    else:
        age = _since(record.get("last_declared_at"), now)
    return f"{doing.strip()} ({age} ago)"


def _supervisor_for(front: str, roster: dict) -> tuple[str, dict] | None:
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
                    for record in _open_anomalies(
                        store.read_ledger(paths.anomalies_path())))
    except OSError:
        stale = False
    if stale:
        tick += " \u00b7 collector stale \u2014 foreman collector restart"
    return (f"Foreman status \u2014 {registered} sessions registered, "
            f"{seen} observed \u00b7 {frozen} \u00b7 {tick}")


def _needs_you(now: datetime) -> list[str]:
    items = [item for item in read_inbox()[0]
             if item.get("answered_at") is None]
    items.sort(key=lambda item: item.get("asked_at") or "")
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
    open_lines = _open_anomalies(records)
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
        return f"{role} job {state} {_since(stamp, now)} ago"
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
    # A task that was moved backwards says so until it moves again: the
    # reset is the owner's business, not a quiet correction.
    if task.get("reset_reason"):
        head += f" \u00b7 reset: {task.get('reset_reason')}"
    return head


def _load_fronts() -> tuple[list[tuple[str, list[dict], list[dict]]],
                          dict[str, str], dict[str, str]]:
    """Every front with its folded tasks and jobs, plus the global
    task-title and session->task-name maps both Working and Problems read."""
    loaded: list[tuple[str, list[dict], list[dict]]] = []
    titles: dict[str, str] = {}
    jobs_by_session: dict[str, str] = {}
    for name in _fronts():
        tasks, jobs = _tasks(name), _jobs(name)
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


def _working(roster: dict, observed: dict | None, now: datetime,
             loaded: list[tuple[str, list[dict], list[dict]]]) -> list[str]:
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
        front_record = _front_record(name)
        # A front for trying the runtime out says so on every line it owns:
        # a task a probe job moved must never read as work a front did.
        label = name
        if (front_record or {}).get("fixture"):
            label = f"{name} (fixture)"
        if held is None:
            lines.append(f"  {label} \u2014 no supervisor \u00b7 {progress}")
        else:
            sid, record = held
            doing = _doing_line(sid, record, sessions_view, now)
            tail = f" \u00b7 doing now: {doing}" if doing else ""
            lines.append(f"  {label} \u2014 supervisor attached "
                         f"\u00b7 {progress}{tail}")
        if front_record is not None:
            done_when = front_record.get("done_when") or ""
            if isinstance(done_when, str) and done_when.strip():
                lines.append(f"    {done_when.strip()}")
            allocation = front_record.get("allocation") or {}
            if isinstance(allocation, dict) and allocation:
                open_grants = _slots_held()
                parts = [f"{role} {open_grants.get((name, role), 0)}/"
                         f"{allocation[role]}"
                         for role in sorted(allocation)]
                lines.append(f"    allocation: {', '.join(parts)}")
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
            lines.append(f"    evidence: {len(evidence)} "
                         f"({confirmed} confirmed) "
                         f"\u00b7 findings: {len(findings)}")
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
    return lines


def _job_queue(now: datetime) -> list[str]:
    waiting: list[str] = []
    for name in _fronts():
        tasks = {task.get("id"): task.get("title") for task in _tasks(name)}
        for job in _jobs(name):
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
    merges = _merges()
    if merges is None:
        return ["Merge queue: empty."]
    waiting = [row for row in merges if row.get("landed_at") is None]
    landed = [row for row in merges if row.get("landed_at") is not None]
    if not waiting and not landed:
        return ["Merge queue: empty."]
    lines = ["Merge queue:"]
    for row in waiting:
        tasks = ", ".join(titles.get(task, task)
                          for task in (row.get("tasks") or []))
        lines.append(f"  waiting: {row.get('branch')} -> "
                     f"{row.get('target')}, lands {tasks} "
                     f"(requested {_since(row.get('requested_at'), now)} "
                     f"ago)")
    if landed:
        landed.sort(key=lambda row: row.get("landed_at") or "")
        last = landed[-1]
        lines.append(f"  last landed: {last.get('branch')} -> "
                     f"{last.get('target')} "
                     f"({_since(last.get('landed_at'), now)} ago)")
    return lines


def _capacity(observed: dict | None) -> list[str]:
    """Held/cap per pool, held/ceiling per allocated front role, and the
    jobs waiting on a full pool — built from the slot ledger and the front
    records, so it is right whether or not the collector has ticked. The
    whole computation lives in :mod:`foreman.capacity`, imported here and
    not at the top so this module keeps the import list it had."""
    from . import capacity

    return capacity.capacity_lines(observed)


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
    blocks = [_header(roster, observed, moment),
              *_needs_you(moment),
              *_problems(roster, jobs_by_session, moment),
              *_working(roster, observed, moment, loaded),
              *_job_queue(moment),
              *_merge_queue(titles, moment),
              *_capacity(observed)]
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

