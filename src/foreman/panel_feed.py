"""`foreman panel-feed`: the panel's eight blocks, folded once, as one file.

The panel used to read the state directory itself: one watched file per
ledger, six more per front, four more per session, and every load rebuilt
all eight blocks over all fronts and all sessions. The files are small and
the fold is cheap; the fan-out was not, and it ran on the shell's main
thread.

So the runtime folds instead. This module reads the same bytes
:mod:`foreman.status` reads, through the same gathering functions, and
writes ``panel.json`` in the state directory: the eight blocks of
docs/DESIGN.md section 13 with every fact the panel renders already
derived. The panel watches that one file and parses nothing else.

The fold here is the panel's, not the text screen's. Both views read one
state directory through one set of gatherers, but they answer different
questions — the screen prints sentences with an ESTIMATE from finished
jobs, the panel draws cards with a rate from unlanded units — so the
derivations stay apart and only the reading is shared.

Two clocks. Every age in a block is seconds from the collector's own tick
(``observed.json``'s ``at``) to the stamp on the row, so the whole screen
is measured from one instant and freezes correctly in the file. The one
exception is the collector's own age, which is measured against the wall
clock: the file carries ``collectorAt`` and the panel ages it itself, so a
collector that dies stops reading fresh instead of freezing at "alive".

Written atomically: staged beside the target and renamed, so a panel
reading while the collector writes sees the old file or the new one and
never half of either.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import caller, paths, status, store
from .cli import subcommand

#: A turn marker counts as a running turn while it is fresher than this.
#: The panel showed the same number and could not read the configured one.
TURN_STALE_S = 600

#: The collector is alive while its last tick is younger than this.
COLLECTOR_ALIVE_S = 120

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Clock. The panel measured every age with JavaScript's Date.parse, and the
# harness pins the numbers it produced, so this parses the way that does:
# a stamp carrying an offset is that instant, a bare date is UTC, and a bare
# date-time is local. Nothing in the runtime writes a stamp without an
# offset; a hand-edited ledger that does still reads as the panel read it.
# ---------------------------------------------------------------------------


def _ms(text: object) -> int | None:
    """Milliseconds since the epoch, or None for anything unparsable."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(
            tzinfo=timezone.utc if _DATE_ONLY.match(text)
            else datetime.now().astimezone().tzinfo)
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def _seconds(from_iso: object, now_iso: object) -> float | None:
    """Seconds from ``from_iso`` to ``now_iso``, or to the wall clock when
    ``now_iso`` is empty. None when either stamp will not parse."""
    start = _ms(from_iso)
    if start is None:
        return None
    if now_iso:
        end = _ms(now_iso)
        if end is None:
            return None
    else:
        end = (datetime.now(timezone.utc) - _EPOCH) // timedelta(
            milliseconds=1)
    return (end - start) / 1000


def _iso_ms(milliseconds: float) -> str:
    """An instant as the panel wrote it: UTC with milliseconds and a Z."""
    whole = int(milliseconds)
    seconds, msec = divmod(whole, 1000)
    moment = _EPOCH + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % msec


def _number(value: object) -> bool:
    """True for a JSON number. A bool is not a number, here or in JSON."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value: object) -> str:
    """A field the panel rendered as a string: anything else reads empty."""
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Reading. Every path comes from paths.py and every fold from the gathering
# status.py shares; the tolerant readers below only add what a file being
# appended to while it is read needs — a half-written last line is dropped,
# never raised, because the panel must keep drawing.
# ---------------------------------------------------------------------------


def _records(path: Path) -> list[dict]:
    """Every line of a ledger, unfolded, tolerating a torn tail."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _fold_by(records: list[dict], key_of) -> list[dict]:
    """Last line wins, first appearance keeps its place."""
    order: list[str] = []
    by_key: dict[str, dict] = {}
    for record in records:
        key = key_of(record)
        if not key:
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = record
    return [by_key[key] for key in order]


def _fold_by_id(records: list[dict]) -> list[dict]:
    return _fold_by(records, lambda record: _text(record.get("id")))


def _open(rows: list[dict], field: str) -> list[dict]:
    """Rows whose field is still unset: unanswered, unresolved, unlanded."""
    return [row for row in rows if row.get(field) in (None, "")]


# ---------------------------------------------------------------------------
# The blocks.
# ---------------------------------------------------------------------------


class _World:
    """One consistent read of the state directory, folded once.

    Every block below derives from this and from nothing else, so the panel
    never shows a header from this tick beside a queue from the last one.
    """

    def __init__(self) -> None:
        # The panel listed front directories itself and skipped dotted
        # names; the gathering status.py shares lists the same ones.
        self.fronts = [name for name in status.front_names()
                       if not name.startswith(".")]
        self.roster = caller.read_roster().get("sessions", {})
        if not isinstance(self.roster, dict):
            self.roster = {}
        observed = store.read_snapshot(paths.observed_path(), default=None)
        self.observed = observed if isinstance(observed, dict) else None
        self.now = _text((self.observed or {}).get("at"))
        self.frozen = paths.frozen_path().exists()
        self.inbox = _fold_by_id(_records(paths.inbox_path()))
        self.open_inbox = _open(self.inbox, "answered_at")
        self.merges = _fold_by_id(_records(paths.merges_path()))
        self.anomalies = status.open_anomalies(
            _records(paths.anomalies_path()))
        self.slots = _records(paths.slots_path())
        self.observed_fronts = (self.observed or {}).get("fronts") or {}
        self.observed_jobs = (self.observed or {}).get("jobs") or {}
        self.pools = (self.observed or {}).get("pools") or {}

    def seconds(self, stamp: object) -> float | None:
        return _seconds(stamp, self.now)

    def pool_for(self, role: str) -> str:
        """Which pool a role waits on: the slot ledger's mapping, later
        lines winning; a role with no line waits on the pool of its name."""
        pool = ""
        for line in self.slots:
            if line.get("role") == role and _text(line.get("pool")):
                pool = line["pool"]
        return pool or role

    def supervisor_for(self, front: str) -> str:
        """The front's supervisor: a running one wins, else the first the
        roster names. Roster order is the order the file holds."""
        backup = ""
        for sid, record in self.roster.items():
            if not isinstance(record, dict):
                continue
            if record.get("role") != "supervisor" or \
                    record.get("front") != front:
                continue
            if record.get("state") == "running":
                return sid
            if backup == "":
                backup = sid
        return backup

    def blocked_on_owner(self, front: str) -> list[str]:
        """Open questions from this front, joined through the asker's roster
        row so a worker's question counts, in inbox order."""
        if not front:
            return []
        out = []
        for item in self.open_inbox:
            sender = self.roster.get(_text(item.get("from"))) or {}
            if isinstance(sender, dict) and sender.get("front") == front:
                out.append(_text(item.get("question")))
        return out


def _header(world: _World) -> dict:
    swarm = (world.observed or {}).get("swarm") or {}
    registered = swarm.get("sessions_registered")
    if not _number(registered):
        registered = len(world.roster)
    seen = swarm.get("sessions_observed")
    if not _number(seen):
        seen = 0
    age = _seconds(world.now, "") if world.observed else None
    inbox_depth = swarm.get("inbox_depth")
    open_count = swarm.get("open_anomalies")
    return {
        "count": registered,
        "registered": registered,
        "observed": seen,
        "frozen": world.frozen,
        # The panel ages this one itself against its own clock; see the
        # module docstring.
        "collectorAt": world.now,
        "collectorAgeS": age,
        "collectorAlive": age is not None and age < COLLECTOR_ALIVE_S,
        "inboxDepth": inbox_depth if _number(inbox_depth) else 0,
        "openAnomalies": open_count if _number(open_count) else 0,
        "rows": [
            {"label": "sessions",
             "value": f"{registered} registered, {seen} observed"},
            {"label": "frozen",
             "value": "frozen" if world.frozen else "not frozen"},
            {"label": "collector",
             "value": "never ticked" if age is None else age},
        ],
    }


def _needs_you(world: _World) -> dict:
    rows = []
    for item in sorted(world.open_inbox,
                       key=lambda item: _text(item.get("asked_at"))):
        rows.append({
            "id": _text(item.get("id")),
            "question": _text(item.get("question")),
            "recommendation": _text(item.get("recommendation")),
            "kind": _text(item.get("kind")),
            "from": _text(item.get("from")),
            "options": item.get("options") or [],
            "waitedS": world.seconds(item.get("asked_at")),
        })
    return {"count": len(rows), "rows": rows}


def _action_for_anomaly(row: dict) -> str:
    """The verb an anomaly names outright. The collector's ledger carries
    no action, so a silent or dead supervisor is relaunched from its
    checkpoint and an intruder is killed; a kind whose verb needs a job id
    the anomaly does not carry stays keyless rather than guessing one."""
    explicit = _text(row.get("action"))
    if explicit:
        return explicit
    kind, subject = _text(row.get("kind")), _text(row.get("subject"))
    if not subject:
        return ""
    if kind in ("supervisor silent", "supervisor dead"):
        return "foreman relaunch " + subject
    if kind == "intruder":
        return "foreman kill " + subject
    return ""


def _problems(world: _World) -> dict:
    rows = []
    for row in world.anomalies:
        session = world.roster.get(_text(row.get("subject"))) or {}
        front = session.get("front") if isinstance(session, dict) else ""
        rows.append({
            "kind": _text(row.get("kind")),
            "subject": _text(row.get("subject")),
            "detail": _text(row.get("detail")),
            "front": _text(front),
            "action": _action_for_anomaly(row),
            "sinceS": world.seconds(row.get("since")),
        })
    return {"count": len(rows), "rows": rows}


def _job_row(world: _World, job: dict) -> dict:
    seen = world.observed_jobs.get(_text(job.get("id"))) or {}
    if not isinstance(seen, dict):
        seen = {}
    session = world.roster.get(_text(job.get("session"))) or {}
    if not isinstance(session, dict):
        session = {}
    elapsed = seen.get("elapsed_s")
    timeout = seen.get("timeout_s")
    idle = seen.get("minutes_since_write")
    return {
        "id": _text(job.get("id")),
        "task": _text(job.get("task")),
        "kind": _text(job.get("kind")),
        "role": _text(job.get("role")),
        "model": _text(session.get("model")) or "unknown",
        "state": "stalled" if session.get("state") == "stalled"
                 else _text(job.get("state")),
        "worktree": _text(job.get("worktree")),
        "branch": _text(job.get("branch")),
        "elapsedS": elapsed if _number(elapsed) else None,
        "timeoutS": timeout if _number(timeout) else None,
        "minutesSinceWrite": idle if _number(idle) else None,
        "waitedS": world.seconds(job.get("queued_at")
                                 or job.get("planned_at")),
    }


def _monitor_rows(world: _World, monitors: list[dict]) -> list[dict]:
    return [{
        "monitor": _text(row.get("monitor")),
        "value": row.get("value"),
        "of": row.get("of"),
        "trend": _text(row.get("trend")),
        "status": _text(row.get("status")),
        "measuredS": world.seconds(row.get("at")),
    } for row in monitors]


def _headless_facts(world: _World, supervisor: str) -> dict:
    """What a headless supervisor's own ledgers say: the last wake, the
    turn count and last turn, and whether a turn is running. A windowed
    session carries none of it, and the line reads as it always has."""
    empty = {"isHeadless": False, "wakeReason": "", "wakeAgeS": None,
             "turnCount": 0, "lastTurnS": None, "turnRunning": False}
    record = world.roster.get(supervisor) or {}
    if not isinstance(record, dict) or record.get("headless") is not True:
        return empty

    def latest(rows: list[dict]) -> dict | None:
        best = None
        for row in rows:
            at = _text(row.get("at"))
            if not at:
                continue
            if best is None or at > best["at"]:
                best = row
        return best

    wake = latest(_records(paths.session_events_path(supervisor)))
    turns = _records(paths.session_turns_path(supervisor))
    last = latest(turns)
    marker = store.read_snapshot(paths.session_turn_path(supervisor),
                                 default={})
    if not isinstance(marker, dict):
        marker = {}
    started = world.seconds(marker.get("started_at")) \
        if _text(marker.get("started_at")) else None
    return {
        "isHeadless": True,
        "wakeReason": _text((wake or {}).get("reason")),
        "wakeAgeS": world.seconds((wake or {}).get("at"))
        if _text((wake or {}).get("at")) else None,
        "turnCount": len(turns),
        "lastTurnS": world.seconds((last or {}).get("at"))
        if _text((last or {}).get("at")) else None,
        "turnRunning": started is not None and started <= TURN_STALE_S,
    }


def _progress_rate(world: _World, history: list[dict],
                   task_rows: list[dict]) -> dict:
    """Rate and projected finish from what the task ledger records.

    A task line's ``at`` is the task's creation stamp — a rewrite carries it
    forward unchanged — so the ledger says how far the work has got, never
    when it moved there. What it does record is output so far (each unlanded
    task's units_done) and how long that work has existed (its oldest
    creation stamp to the collector clock): output over age is the rate, and
    the age span in hours is the window. Landed work is off the board.
    """
    empty = {"ratePerHour": None, "windowH": None, "projectedFinish": ""}
    open_ids = {row["id"] for row in task_rows
                if row["state"] != "landed" and row["id"]}
    start = None
    for line in history:
        if _text(line.get("id")) not in open_ids:
            continue
        moment = _ms(line.get("at"))
        if moment is None:
            continue
        if start is None or moment < start:
            start = moment
    now_ms = _ms(world.now)
    if start is None or now_ms is None or now_ms <= start:
        return empty
    done = remaining = 0
    for row in task_rows:
        if row["state"] == "landed":
            continue
        done += row["unitsDone"]
        left = row["unitsTotal"] - row["unitsDone"]
        if left > 0:
            remaining += left
    window_h = (now_ms - start) / 3600000
    if done <= 0:
        return {"ratePerHour": 0, "windowH": window_h, "projectedFinish": ""}
    rate = done / window_h
    projected = ""
    if remaining > 0:
        projected = _iso_ms(now_ms + (remaining / rate) * 3600000)
    return {"ratePerHour": rate, "windowH": window_h,
            "projectedFinish": projected}


def _units(value: object) -> int:
    return value if _number(value) else 0


def _front_rows(world: _World) -> tuple[list[dict], list[dict],
                                        list[dict], dict[str, str]]:
    """Every front as one row, split into what is running and what waits.

    Working and the front queue come out of the same walk: a front is
    either running (its supervisor is on it) or waiting to start.
    """
    working: list[dict] = []
    queued: list[dict] = []
    everything: list[dict] = []
    titles: dict[str, str] = {}

    for name in world.fronts:
        record = status.front_record_of(name) or {}
        derived = world.observed_fronts.get(name) or {}
        if not isinstance(derived, dict):
            derived = {}
        tasks = status.front_tasks(name)
        history = _records(paths.front_tasks_path(name))
        jobs = status.front_jobs(name)

        landed = built = 0
        remaining: list[str] = []
        task_rows: list[dict] = []
        for task in tasks:
            tid, title = _text(task.get("id")), _text(task.get("title"))
            if tid:
                titles[tid] = title or tid
            # Section 13 wants every task not landed in remaining, so the
            # not-landed filter and the built count are independent: a
            # built task is counted AND listed.
            if task.get("state") == "landed":
                landed += 1
            else:
                remaining.append(title or tid)
                if task.get("state") == "built":
                    built += 1
            # A job references its task by id or by title: the runtime
            # writes both, so a title-referenced job still lands on it.
            task_rows.append({
                "id": tid,
                "title": title,
                "state": _text(task.get("state")),
                "unitsDone": _units(task.get("units_done")),
                "unitsTotal": _units(task.get("units_total")),
                "after": task.get("after") or [],
                "jobs": [_job_row(world, job) for job in jobs
                         if job.get("task") == tid
                         or (title and job.get("task") == title)],
            })

        supervisor = world.supervisor_for(name)
        # Doing-now lives in the supervisor's checkpoint, not in observed:
        # the collector writes no fronts key. observed.fronts is only the
        # fallback that keeps the mock fixture rendering.
        checkpoint = status.session_checkpoint(supervisor) or {} \
            if supervisor else {}
        headless = _headless_facts(world, supervisor)
        progress = _progress_rate(world, history, task_rows)
        rate = progress["ratePerHour"]
        if rate is None and _number(derived.get("rate_per_hour")):
            rate = derived["rate_per_hour"]
        row = {
            "name": name,
            "want": _text(record.get("want")),
            "state": _text(record.get("state")),
            "landOn": _text(record.get("land_on")),
            "supervisor": supervisor,
            "isHeadless": headless["isHeadless"],
            "wakeReason": headless["wakeReason"],
            "wakeAgeS": headless["wakeAgeS"],
            "turnCount": headless["turnCount"],
            "lastTurnS": headless["lastTurnS"],
            "turnRunning": headless["turnRunning"],
            "doingNow": _text(checkpoint.get("doing"))
                        or _text(derived.get("doing_now")),
            "doingNext": _text(checkpoint.get("next")),
            "doingAgeS": derived["doing_age_s"]
            if _number(derived.get("doing_age_s")) else None,
            "ratePerHour": rate,
            "rateWindowH": progress["windowH"],
            "projectedFinish": progress["projectedFinish"]
                               or _text(derived.get("projected_finish")),
            "landed": landed,
            "built": built,
            "total": len(tasks),
            "remaining": remaining,
            "blockedOnOwner": world.blocked_on_owner(name),
            "tasks": task_rows,
            "monitors": _monitor_rows(world, _fold_by(
                _records(paths.front_measurements_path(name)),
                lambda line: _text(line.get("monitor")))),
            # Not a block fact: the job queue walks the same jobs, and
            # re-reading the ledger for it would double the work.
            "_jobs": jobs,
        }
        everything.append(row)
        if record.get("state") == "queued":
            after = record.get("after") or []
            queued.append({
                "name": name,
                "want": _text(record.get("want")),
                "order": record["order"] if _number(record.get("order"))
                else 0,
                "prefer": record["prefer"] if _number(record.get("prefer"))
                else 0,
                "after": after,
                "waitsFor": ("after " + ", ".join(str(a) for a in after))
                            if after else "capacity",
            })
        elif record.get("state") != "done":
            working.append(row)

    queued.sort(key=lambda row: (-row["prefer"], row["order"]))
    return working, queued, everything, titles


def _job_queue(world: _World, fronts: list[dict]) -> dict:
    """What a supervisor planned and cannot start yet, in the order that
    supervisor wrote it, oldest wait first across fronts."""
    rows = []
    for front in fronts:
        titles = {row["id"]: row["title"] for row in front["tasks"]
                  if row["id"]}
        for job in front["_jobs"]:
            if job.get("state") not in status.QUEUE_STATES:
                continue
            row = _job_row(world, job)
            row["front"] = front["name"]
            row["title"] = titles.get(row["task"]) or row["task"]
            rows.append(row)
    rows.sort(key=lambda row: -(row["waitedS"] if row["waitedS"] is not None
                                else -1))
    return {"count": len(rows), "rows": rows}


def _merge_queue(world: _World, titles: dict[str, str]) -> dict:
    rows, merging, waiting, landed = [], [], [], []
    for record in world.merges:
        tasks = record.get("tasks") or []
        state = "landed" if record.get("landed_at") else (
            "merging" if record.get("result") == "merging" else "waiting")
        row = {
            "id": _text(record.get("id")),
            "branch": _text(record.get("branch")),
            "target": _text(record.get("target")),
            "from": _text(record.get("from")),
            "tasks": [titles.get(task, task) for task in tasks],
            "reviews": record.get("review_refs") or [],
            "state": state,
            "requestedS": world.seconds(record.get("requested_at")),
            "landedS": world.seconds(record.get("landed_at"))
            if record.get("landed_at") else None,
        }
        rows.append(row)
        (landed if state == "landed"
         else merging if state == "merging" else waiting).append(row)
    return {"count": len(rows), "rows": rows, "merging": merging,
            "waiting": waiting, "landed": landed}


def _capacity(world: _World, queued_jobs: list[dict]) -> dict:
    waiting_by: dict[str, int] = {}
    for job in queued_jobs:
        pool = world.pool_for(job["role"])
        waiting_by[pool] = waiting_by.get(pool, 0) + 1
    rows = []
    for name in sorted(world.pools):
        entry = world.pools.get(name) or {}
        if not isinstance(entry, dict):
            entry = {}
        rows.append({
            "pool": name,
            "held": entry["held"] if _number(entry.get("held")) else 0,
            "total": entry["total"] if _number(entry.get("total")) else None,
            "meter": entry["meter"] if _number(entry.get("meter")) else None,
            "resetsAt": _text(entry.get("resets_at")),
            "avgS": entry["avg_s"] if _number(entry.get("avg_s")) else None,
            "p90S": entry["p90_s"] if _number(entry.get("p90_s")) else None,
            "waiting": waiting_by.get(name, 0),
        })
    return {"count": len(rows), "rows": rows}


def gather() -> dict:
    """Every fact the panel's eight blocks render, folded once."""
    world = _World()
    working, queued, everything, titles = _front_rows(world)
    job_queue = _job_queue(world, everything)
    for row in everything:
        del row["_jobs"]
    return {
        "at": store.utcnow_iso(),
        "now": world.now,
        "frozen": world.frozen,
        "header": _header(world),
        "needsYou": _needs_you(world),
        "problems": _problems(world),
        "working": {"count": len(working), "rows": working},
        "jobQueue": job_queue,
        "frontQueue": {"count": len(queued), "rows": queued},
        "mergeQueue": _merge_queue(world, titles),
        "capacity": _capacity(world, job_queue["rows"]),
        # Every front, running or queued or done: the Working block draws
        # the running ones, and the verification harness reads the rest.
        "fronts": everything,
        # The two joins the panel's own components make from a session id:
        # which front a question came from, and whether a supervisor is
        # stalled. Nothing else of the roster reaches the panel.
        "roster": {sid: {"front": _text(record.get("front")),
                         "role": _text(record.get("role")),
                         "state": _text(record.get("state"))}
                   for sid, record in world.roster.items()
                   if isinstance(record, dict)},
        # Role-to-pool, which the capacity and job-queue blocks read.
        "slots": [{"role": line.get("role"), "pool": line.get("pool")}
                  for line in world.slots],
    }


def write() -> Path:
    """Fold the state directory and replace panel.json with the result."""
    target = paths.panel_path()
    store.write_snapshot(target, gather())
    return target


def rewrite_quietly() -> None:
    """Refresh panel.json without ever failing the caller.

    Every verb that wrote a ledger calls this on its way out and the
    collector calls it on every tick. The file derives from the ledgers and
    holds nothing of its own, so a rewrite that cannot run leaves the panel
    one tick stale and nothing else: it must never turn a verb that worked
    into a verb that reported failure.
    """
    try:
        write()
    except Exception:  # noqa: BLE001 - a summary is never worth a refusal
        pass


@subcommand("panel-feed",
            help="Write panel.json: the panel's eight blocks, folded once.")
def _panel_feed_entry(args: argparse.Namespace) -> int:
    return panel_feed_main(args.fixture)


def add_panel_feed_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fixture", default=None,
                        help="read and write a state directory at this path "
                             "instead of the real one")


_panel_feed_entry.add_arguments = add_panel_feed_arguments  # type: ignore[attr-defined]


def panel_feed_main(fixture: str | None = None) -> int:
    if fixture is None:
        print(write())
        return 0
    if not Path(fixture).is_dir():
        print(f"foreman panel-feed: no such fixture directory: {fixture}")
        return 1
    previous = os.environ.get(paths.STATE_ENV)
    os.environ[paths.STATE_ENV] = fixture
    try:
        print(write())
    finally:
        if previous is None:
            del os.environ[paths.STATE_ENV]
        else:
            os.environ[paths.STATE_ENV] = previous
    return 0
