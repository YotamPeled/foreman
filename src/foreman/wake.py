"""Wake events: the ledger the collector writes, the queue turns read.

Each session owns ``state/sessions/<session>/events.jsonl``, append-only
and folded last-wins by id like every other ledger. One record per event
with a ``wke-`` id, the session it is for, the reason, the moment, and
the ids the reason names. Delivery is a revised copy of the same line
with ``delivered_at`` set, so the fold proves an event woke its session
exactly once.

A wake is never delivered while a turn of that session is running: a
queued event waits for the turn to end, then one wake carries every
event queued during it. The turn marker is a small file with a start
time and, once the vendor process exists, that process's pid and
starttime — the same identity a launch records — so the collector can
claim the tree. It is cleared by staleness rather than held as an
unbounded flag, so a crashed turn cannot wedge the queue for ever.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from . import caller, cli, entities, ids, paths, store
from .caller import FOREMAN, MERGE_DESK, OWNER, SUPERVISOR, Refusal

#: Sessions that run turns and therefore keep a wake queue: workers are
#: one vendor process, not turns, so only these hold the live contract
#: a heartbeat watches.
WAKEFUL_ROLES = ("supervisor", "foreman", "merge-desk")


def _now_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


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


def append_event(session_id: str, reason: str,
                 now: datetime | str | None = None, **fields) -> dict:
    """Append one undelivered event for ``session_id``. Returns the line.

    The reason must be one the vocabulary names. A turn decides what it
    is about by reading the reason, so a misspelled one is a wake no
    session can act on: refuse it here rather than write it and find out
    a turn later.
    """
    if reason not in entities.WAKE_REASONS:
        raise ValueError(
            f"unknown wake reason {reason!r}; the reasons are "
            + ", ".join(entities.WAKE_REASONS))
    at = now if isinstance(now, str) else _now_iso(now)
    record = entities.WakeEvent(
        id=ids.mint("wake"), session=session_id, reason=reason, at=at,
        **fields,
    ).to_dict()
    return store.append_ledger(paths.session_events_path(session_id), record)


def read_events(session_id: str) -> list[dict]:
    """The session's ledger folded last-wins, in first-appearance order."""
    try:
        records = store.read_ledger(paths.session_events_path(session_id))
    except OSError:
        return []
    return store.fold_by_id(records)


def pending_events(session_id: str) -> list[dict]:
    """Folded events no wake has carried yet."""
    return [record for record in read_events(session_id)
            if record.get("delivered_at") is None]


def last_wake_at(session_id: str) -> str | None:
    """The latest delivery moment, or None where no wake ever went out."""
    latest: str | None = None
    for record in read_events(session_id):
        stamped = record.get("delivered_at")
        if isinstance(stamped, str) and stamped and \
                (latest is None or stamped > latest):
            latest = stamped
    return latest


def _turn_stale_seconds() -> float:
    try:
        from .collector import load_config
    except ImportError:  # pragma: no cover - the module is always present
        return 600.0
    try:
        return float(load_config().turn_stale_seconds)
    except (OSError, ValueError, TypeError):
        return 600.0


def turn_started_at(session_id: str) -> datetime | None:
    """When the session's turn marker says its turn started, or None.

    Read-only: a reader (the status screen) must never clear a stale
    marker by looking at it, so the unlink lives in :func:`turn_running`
    and nowhere else.
    """
    marker = store.read_snapshot(paths.session_turn_path(session_id),
                                 default=None)
    if not isinstance(marker, dict):
        return None
    return _parse_time(marker.get("started_at"))


def turn_fresh(session_id: str, now: datetime | None = None) -> bool:
    """True while a fresh turn marker exists. Never writes.

    The read-only half of :func:`turn_running`: a stale marker reads as
    no turn running but is left on disk for the turn path to clear.
    """
    started = turn_started_at(session_id)
    if started is None:
        return False
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - started).total_seconds() <= _turn_stale_seconds()


def turn_running(session_id: str, now: datetime | None = None) -> bool:
    """True while a fresh turn marker exists. A stale marker clears itself."""
    if turn_fresh(session_id, now=now):
        return True
    if turn_started_at(session_id) is not None:
        try:
            paths.session_turn_path(session_id).unlink()
        except OSError:
            pass
    return False


def last_wake(session_id: str) -> dict | None:
    """The session's latest wake event by its ``at`` stamp, or None.

    Delivered or still queued: either way the session was woken, and the
    status screen names the reason of this event. None where no event
    ever named the session.
    """
    latest: dict | None = None
    for record in read_events(session_id):
        at = record.get("at")
        if not isinstance(at, str) or not at:
            continue
        if latest is None or at > (latest.get("at") or ""):
            latest = record
    return latest


def turn_started(session_id: str, now: datetime | None = None,
                 pid: int | None = None,
                 pid_starttime: int | None = None) -> None:
    """Mark a turn running.

    A pid already stamped for this turn stands: the shell may have
    recorded it before this write lands, and overwriting the marker
    without that pid is how a whole turn's vendor looks like an
    intruder. A missing marker starts clean (no pid unless this call
    supplies one). ``pid`` and ``pid_starttime`` are the process the
    turn owns, recorded the way a launch records them; omitted until
    the process exists, :func:`record_turn_pid` fills them in without
    resetting ``started_at``.
    """
    def change(current):
        marker: dict = {"session": session_id, "started_at": _now_iso(now)}
        if isinstance(current, dict):
            old_pid = current.get("pid")
            if isinstance(old_pid, int) and old_pid > 0:
                marker["pid"] = old_pid
            if "pid_starttime" in current:
                marker["pid_starttime"] = current["pid_starttime"]
        if isinstance(pid, int) and pid > 0:
            marker["pid"] = pid
        if pid_starttime is not None:
            marker["pid_starttime"] = pid_starttime
        return marker
    store.update_snapshot(paths.session_turn_path(session_id), change,
                          default=None)


def record_turn_pid(session_id: str, pid: int,
                    pid_starttime: int | None = None) -> None:
    """Stamp the live turn marker with the process the turn started.

    The shell that runs this is the turn, so a missing marker is a race
    against :func:`turn_started`, not an absence: write the claim
    anyway. Keep ``started_at`` when a marker already exists.
    ``pid_starttime`` is the process identity the collector matches;
    omitted, it is read off the process now. A pid that cannot be
    identified (gone, or no starttime) is not recorded, so a recycled
    pid cannot inherit the claim.
    """
    if not isinstance(pid, int) or pid <= 0:
        return
    from . import procs
    start = pid_starttime if pid_starttime is not None \
        else procs.proc_starttime(pid)
    if start is None:
        return

    def change(current):
        if isinstance(current, dict):
            marker = dict(current)
        else:
            marker = {"session": session_id, "started_at": _now_iso()}
        marker.setdefault("session", session_id)
        marker.setdefault("started_at", _now_iso())
        marker["pid"] = pid
        marker["pid_starttime"] = start
        return marker
    store.update_snapshot(paths.session_turn_path(session_id), change,
                          default=None)


def turn_ended(session_id: str) -> None:
    """Mark the turn over. A session with no marker stays without one."""
    try:
        paths.session_turn_path(session_id).unlink()
    except OSError:
        pass


def claim_wake(session_id: str,
               now: datetime | None = None) -> list[dict] | None:
    """Claim every queued event as one wake, marking each delivered.

    Returns the wake (possibly empty) or None while a turn is running:
    a queued event waits for the turn to end, then one wake carries
    every event queued during it.
    """
    if turn_running(session_id, now=now):
        return None
    queued = pending_events(session_id)
    if not queued:
        return []
    stamped = _now_iso(now)
    for record in queued:
        store.append_ledger(paths.session_events_path(session_id),
                            dict(record, delivered_at=stamped))
    return queued


def emit_job_event(front: str | None, job_id: str | None,
                   latest: dict | None, reason: str,
                   now_iso: str) -> dict | None:
    """Wake the job's launcher about its outcome. None where nobody wakes.

    The launcher is the ``launched_by`` on the roster session running
    the job; an owner launch names nobody, and the owner watches the
    screen, not a ledger. ``latest`` is the job line just appended, so
    the event names the job, the front and the task without re-reading
    the ledger.
    """
    if not front or not job_id or latest is None:
        return None
    target: str | None = None
    try:
        sessions = caller.read_roster().get("sessions", {})
    except OSError:
        sessions = {}
    if isinstance(sessions, dict):
        for record in sessions.values():
            if isinstance(record, dict) and record.get("job") == job_id:
                launched_by = record.get("launched_by")
                if isinstance(launched_by, str) and launched_by \
                        and launched_by != OWNER:
                    target = launched_by
                break
    if target is None:
        return None
    return append_event(target, reason, now_iso, job=job_id, front=front,
                        task=latest.get("task"))


def emit_inbox_answered(asker: str | None, inbox_id: str, ruling_id: str,
                        now_iso: str) -> dict | None:
    """Wake the session that asked. The owner keeps no ledger to wake."""
    if not asker or asker == OWNER:
        return None
    return append_event(asker, "inbox answered", now_iso, inbox=inbox_id,
                        ruling=ruling_id)


def supervisor_for(front: str | None) -> str | None:
    """The roster's supervisor for ``front``: a live one where there is
    one, else any the roster still names, else None."""
    if not front:
        return None
    try:
        sessions = caller.read_roster().get("sessions", {})
    except OSError:
        return None
    if not isinstance(sessions, dict):
        return None
    from .collector import RUNNING_LIKE

    fallback: str | None = None
    for sid in sorted(sessions):
        record = sessions[sid]
        if not isinstance(record, dict):
            continue
        if record.get("role") != "supervisor" or \
                record.get("front") != front:
            continue
        if fallback is None:
            fallback = sid
        if record.get("state") in RUNNING_LIKE:
            return sid
    return fallback


def emit_rule_landed(front: str, rule_id: str, now_iso: str) -> dict | None:
    """Wake the supervisor of the rule's front. Swarm scope names no
    front and therefore no supervisor to wake."""
    target = supervisor_for(front)
    if target is None:
        return None
    return append_event(target, "rule landed", now_iso, rule=rule_id,
                        front=front)


def desk_for(front: str | None) -> str | None:
    """The roster's merge desk for ``front``: a live one where there is
    one, else any the roster still names, else None.

    The live desk owns no front — one desk holds the swarm's queue — so
    a session with no front serves every front. A desk rostered onto a
    front serves only that front. A roster that names no matching desk
    session returns None, and the caller writes nothing.
    """
    if not front:
        return None
    try:
        sessions = caller.read_roster().get("sessions", {})
    except OSError:
        return None
    if not isinstance(sessions, dict):
        return None
    from .collector import RUNNING_LIKE

    fallback: str | None = None
    for sid in sorted(sessions):
        record = sessions[sid]
        if not isinstance(record, dict):
            continue
        if record.get("role") != MERGE_DESK:
            continue
        held = record.get("front")
        if held not in (None, "", front):
            continue
        if fallback is None:
            fallback = sid
        if record.get("state") in RUNNING_LIKE:
            return sid
    return fallback


def _rostered_session(session_id: str | None) -> str | None:
    """``session_id`` if the roster still names it, else None.

    The owner is not a session and holds no ledger. A session id nobody
    holds is not written to — the heartbeat is the fallback.
    """
    if not session_id or session_id == OWNER:
        return None
    try:
        sessions = caller.read_roster().get("sessions", {})
    except OSError:
        return None
    if not isinstance(sessions, dict) or session_id not in sessions:
        return None
    return session_id


def emit_merge_requested(front: str, merge_id: str, branch: str,
                         now_iso: str) -> dict | None:
    """Wake the front's merge desk. None where the roster names none."""
    target = desk_for(front)
    if target is None:
        return None
    return append_event(target, "merge requested", now_iso, merge=merge_id,
                        front=front, branch=branch)


def emit_merge_landed(front: str, merge_id: str, branch: str,
                      requester: str | None, sha: str,
                      now_iso: str) -> dict | None:
    """Wake the supervisor that asked. None where nobody holds that id."""
    target = _rostered_session(requester)
    if target is None:
        return None
    return append_event(target, "merge landed", now_iso, merge=merge_id,
                        front=front, branch=branch, sha=sha)


def emit_merge_failed(front: str, merge_id: str, branch: str,
                      requester: str | None,
                      now_iso: str) -> dict | None:
    """Wake the supervisor that asked. None where nobody holds that id."""
    target = _rostered_session(requester)
    if target is None:
        return None
    return append_event(target, "merge failed", now_iso, merge=merge_id,
                        front=front, branch=branch)


def heartbeat_tick(moment: datetime, config=None) -> int:
    """Wake idle turn-contract sessions that heard nothing since last wake.

    A session gets a heartbeat only when no other event has woken it
    since its last wake: anything queued will wake it, so a pending
    event of any reason holds the heartbeat back. A session with no
    wake yet counts from its launch. Returns how many heartbeats went
    out.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    minutes = getattr(config, "heartbeat_minutes", 20)
    try:
        interval = float(minutes) * 60
    except (TypeError, ValueError):
        interval = 20 * 60
    if interval <= 0:
        interval = 20 * 60
    try:
        sessions = caller.read_roster().get("sessions", {})
    except OSError:
        return 0
    if not isinstance(sessions, dict):
        return 0
    from .collector import RUNNING_LIKE

    now_iso = moment.isoformat()
    emitted = 0
    for sid in sorted(sessions):
        record = sessions[sid]
        if not isinstance(record, dict):
            continue
        if record.get("role") not in WAKEFUL_ROLES:
            continue
        if record.get("state") not in RUNNING_LIKE:
            continue
        if pending_events(sid):
            continue
        base = last_wake_at(sid) or record.get("started_at")
        moment_base = _parse_time(base)
        if moment_base is None:
            continue
        if (moment - moment_base).total_seconds() < interval:
            continue
        append_event(sid, "heartbeat", now_iso)
        emitted += 1
    return emitted


def _refuse(violations: list[str]) -> int:
    return Refusal(violations).report()


def tell_main(target: str | None, text_parts: list[str]) -> int:
    me, violations = caller.resolve("tell")
    caller.check_role(me, "tell", FOREMAN, violations=violations)
    text = " ".join(text_parts)
    if not (target or "").strip():
        violations.append("field 'session' is required")
    elif me is None or me.role in (OWNER, FOREMAN):
        try:
            known = caller.read_roster().get("sessions", {})
        except OSError:
            known = {}
        if not isinstance(known, dict) or target not in known:
            violations.append(f"unknown session '{(target or '').strip()}'")
    if not text.strip():
        violations.append("field 'text' is required")
    if violations:
        return _refuse(violations)
    assert target is not None
    caller.check_self_contained(text, "tell text")
    event = append_event(target.strip(), "told", from_=caller.by_line(me),
                         text=text.strip())
    print(event["id"])
    return 0


def add_tell_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("session", help="session to wake with one sentence")
    sub.add_argument("text", nargs="*", help="the sentence")


@cli.subcommand("tell", help="Wake a session with one sentence from the foreman.")
def _tell_entry(args: argparse.Namespace) -> int:
    return tell_main(args.session, args.text or [])


_tell_entry.add_arguments = add_tell_arguments  # type: ignore[attr-defined]


def _check_wake_access(me: caller.Caller | None, target: str,
                       verb: str, violations: list[str]) -> None:
    """A session reads and claims its own queue; owner and foreman any.

    Both doors share this helper, so the gate reader lends its roles to
    each caller: only turn-running roles are ever offered the queue, and
    the own-session rule below is what the handler enforces at call time.
    """
    caller.check_role(me, verb, SUPERVISOR, FOREMAN, MERGE_DESK,
                      violations=violations)
    if me is None:
        return
    if me.role == OWNER or me.role == FOREMAN:
        return
    if me.session_id != target:
        violations.append(
            f"session '{me.session_id}' may not claim "
            f"session '{target}'"
        )


def wake_list_main(target: str | None) -> int:
    verb = "wake list"
    me, violations = caller.resolve(verb)
    if not (target or "").strip():
        violations.append("field 'session' is required")
    else:
        assert target is not None
        _check_wake_access(me, target.strip(), verb, violations)
    if violations:
        return _refuse(violations)
    assert target is not None
    for record in read_events(target.strip()):
        print(json.dumps(record, sort_keys=True))
    return 0


def wake_next_main(target: str | None) -> int:
    verb = "wake next"
    me, violations = caller.resolve(verb)
    if not (target or "").strip():
        violations.append("field 'session' is required")
    else:
        assert target is not None
        _check_wake_access(me, target.strip(), verb, violations)
    if violations:
        return _refuse(violations)
    assert target is not None
    wake = claim_wake(target.strip())
    if wake is None:
        return _refuse([f"session '{target.strip()}' has a turn running"])
    if not wake:
        print("no wake")
        return 0
    print(json.dumps(wake, sort_keys=True))
    return 0


def add_wake_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("action", choices=("list", "next"),
                     help="print the ledger, or claim one wake")
    sub.add_argument("session", help="session whose queue this is")


@cli.subcommand("wake", help="Read or claim a session's wake queue.")
def _wake_entry(args: argparse.Namespace) -> int:
    if args.action == "list":
        return wake_list_main(args.session)
    return wake_next_main(args.session)


_wake_entry.add_arguments = add_wake_arguments  # type: ignore[attr-defined]
