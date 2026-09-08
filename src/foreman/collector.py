"""The collector daemon: observe, compute, flag, relaunch.

One tick, every two seconds: read the process table, ask each live
session's pool adapter for its worktree mtime, cpu seconds and finish
marker, write the observed fields back onto the roster snapshot, and
recompute ``observed.json`` (docs/DESIGN.md section 7, only the numbers
v0 has a source for). Then the six anomalies of this version (section
10): supervisor silent, job stalled, job timeout (kill), job tail,
intruder, unregistered writer. Then relaunch dead supervisors from
their checkpoints, guarded against crash loops.

The collector refuses nothing and grants nothing: it observes,
records, kills on timeout and relaunches.

Every threshold comes from ``foreman.toml``; ``DEFAULTS`` below holds
the design's values and is the only place they appear. Clocks are
injected (``tick(now=...)``) so tests never wait on them.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths, procs, store
from .caller import OWNER
from .cli import subcommand
from .entities import Session
from .pools import LaunchContext

#: The design's values, in one place. Call sites read the config object.
DEFAULTS = {
    "tick_seconds": 2,
    "supervisor_silent_seconds": 15 * 60,
    "job_stalled_seconds": 10 * 60,
    "relaunch_limit": 2,
    "relaunch_window_seconds": 60 * 60,
    "vendor_markers": ("muse exec",),
}

WORKER_EXEMPT_ROLES = ("supervisor", "foreman", "owner")
RUNNING_LIKE = ("running", "starting", "stalled")
JOB_RUNNING_LIKE = ("planned", "queued", "running")
REASSERT_KINDS = (
    "supervisor silent",
    "supervisor dead",
    "job stalled",
    "job timeout",
    "job tail",
    "intruder",
)

JOB_LEDGER_NAMES = ("tasks.jsonl", "jobs.jsonl", "evidence.jsonl",
                    "findings.jsonl", "measurements.jsonl")
SCAN_LEDGERS = ("rulings.jsonl", "inbox.jsonl", "slots.jsonl", "merges.jsonl")


@dataclass
class CollectorConfig:
    tick_seconds: float = DEFAULTS["tick_seconds"]
    supervisor_silent_seconds: float = DEFAULTS["supervisor_silent_seconds"]
    job_stalled_seconds: float = DEFAULTS["job_stalled_seconds"]
    relaunch_limit: int = DEFAULTS["relaunch_limit"]
    relaunch_window_seconds: float = DEFAULTS["relaunch_window_seconds"]
    vendor_markers: tuple = field(
        default_factory=lambda: DEFAULTS["vendor_markers"])
    pools_total: dict = field(default_factory=dict)


def load_config(path: str | Path | None = None) -> CollectorConfig:
    """Read thresholds from ``foreman.toml``; the design's value on absence.

    Anything unreadable or misshapen falls back to the default for that
    key, never to a crash: a daemon must not die on its config.
    """
    cfg = CollectorConfig()
    try:
        with open(path or paths.config_file(), "rb") as handle:
            raw = tomllib.load(handle)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    table = raw.get("collector")
    if isinstance(table, dict):
        for key in ("tick_seconds", "supervisor_silent_seconds",
                    "job_stalled_seconds", "relaunch_window_seconds"):
            value = table.get(key)
            if isinstance(value, (int, float)) and value > 0:
                setattr(cfg, key, value)
        limit = table.get("relaunch_limit")
        if isinstance(limit, int) and limit >= 0:
            cfg.relaunch_limit = limit
        markers = table.get("vendor_markers")
        if isinstance(markers, list) and all(
                isinstance(m, str) and m for m in markers):
            cfg.vendor_markers = tuple(markers)
    pools = raw.get("pools")
    if isinstance(pools, dict):
        for name, entry in pools.items():
            if not isinstance(entry, dict):
                continue
            for key in ("slots_total", "slots"):
                total = entry.get(key)
                if isinstance(total, int) and total >= 0:
                    cfg.pools_total[name] = total
                    break
    return cfg


def parse_duration(text: str | None) -> float | None:
    """Seconds for ``20m``-style timeouts (``s``/``m``/``h``/``d``)."""
    if not isinstance(text, str) or not text:
        return None
    total, number, seen = 0.0, "", False
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for char in text:
        if char.isdigit():
            number += char
        elif char in units and number:
            total += int(number) * units[char]
            number, seen = "", True
        else:
            return None
    if number or not seen:
        return None
    return total


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


def _fold_by_id(records: list[dict]) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for record in records:
        rid = record.get("id")
        if isinstance(rid, str) and rid:
            by_id[rid] = record
    return by_id


def _open_map(records: list[dict]) -> dict[tuple[str, str], dict]:
    folded: dict[tuple[str, str], dict] = {}
    for record in records:
        kind, subject = record.get("kind"), record.get("subject")
        if isinstance(kind, str) and isinstance(subject, str):
            folded[(kind, subject)] = record
    return {key: rec for key, rec in folded.items()
            if rec.get("resolved_at") is None}


def _anomaly_line(kind: str, subject: str, since: str, detail: str) -> dict:
    return {"kind": kind, "subject": subject, "since": since, "detail": detail,
            "resolved_at": None}


def read_jobs() -> dict[str, tuple[str, dict]]:
    """Every job ledger line, folded last-wins: id -> (component, record)."""
    folded: dict[str, tuple[str, dict]] = {}
    try:
        components = sorted(Path(paths.components_dir()).iterdir())
    except OSError:
        return folded
    for component in components:
        try:
            records = store.read_ledger(component / "jobs.jsonl")
        except OSError:
            continue
        for record in records:
            jid = record.get("id")
            if isinstance(jid, str) and jid:
                folded[jid] = (component.name, record)
    return folded


def _worktree_mtime(worktree: str | None) -> float | None:
    if not worktree:
        return None
    root = Path(worktree)
    try:
        newest: float | None = root.stat().st_mtime
    except OSError:
        return None
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in filenames:
                try:
                    moment = os.stat(os.path.join(dirpath, name)).st_mtime
                except OSError:
                    continue
                if newest is None or moment > newest:
                    newest = moment
    except OSError:
        pass
    return newest


def _load_state() -> dict:
    state = store.read_snapshot(paths.collector_path(), default=None)
    if not isinstance(state, dict):
        return {"sessions": {}, "relaunches": [], "parents": {}}
    for key, default in (("sessions", {}), ("relaunches", []),
                         ("parents", {})):
        if not isinstance(state.get(key), (dict, list)):
            state[key] = default
    if not isinstance(state["sessions"], dict):
        state["sessions"] = {}
    if not isinstance(state["relaunches"], list):
        state["relaunches"] = []
    if not isinstance(state["parents"], dict):
        state["parents"] = {}
    return state


def _lineage(session_id: str, parents: dict) -> set[str]:
    seen, current = {session_id}, session_id
    while isinstance(parents.get(current), str) and parents[current] not in seen:
        current = parents[current]
        seen.add(current)
    return seen


def _relaunch_count(session_id: str, state: dict, now: datetime,
                    window: float) -> int:
    family = _lineage(session_id, state["parents"])
    count = 0
    for event in state["relaunches"]:
        if not isinstance(event, dict) or event.get("from") not in family:
            continue
        moment = _parse_time(event.get("at"))
        if moment is not None and (now - moment).total_seconds() <= window:
            count += 1
    return count


def _release_slots(session_id: str, now_iso: str) -> None:
    try:
        records = store.read_ledger(paths.slots_path())
    except OSError:
        return
    for record in records:
        if record.get("session") == session_id and \
                record.get("released_at") is None:
            store.append_ledger(paths.slots_path(),
                                dict(record, released_at=now_iso))


def _mark_job(component: str | None, job_id: str | None, to_state: str,
              stamp: str | None) -> None:
    if not component or not job_id:
        return
    try:
        records = store.read_ledger(
            paths.component_jobs_path(component))
    except OSError:
        return
    for record in reversed(records):
        if record.get("id") == job_id and \
                record.get("state") in JOB_RUNNING_LIKE:
            revised = dict(record, state=to_state)
            if stamp is not None and to_state == "returned":
                revised["returned_at"] = stamp
            store.append_ledger(paths.component_jobs_path(component), revised)
            return


def _relaunch_supervisor(sid: str, record: dict, now_iso: str,
                         cstate: dict, config: CollectorConfig) -> str | None:
    """Relaunch a dead supervisor from its checkpoint via its pool adapter.

    Returns the new session id, or None when the launch itself fails.
    """
    from . import ids
    from .pools import get as get_pool

    new_id = ids.mint("session")
    worktree = record.get("worktree")
    if not isinstance(worktree, str) or not os.path.isabs(worktree):
        worktree = str(paths.session_dir(new_id))
    session = Session(
        id=new_id,
        role="supervisor",
        pool=record.get("pool") or "",
        model=record.get("model") or "",
        component=record.get("component"),
        pid=None,
        worktree=worktree,
        log=str(paths.session_log_path(new_id)),
        timeout=record.get("timeout") or "",
        launched_by=sid,
        started_at=now_iso,
        state="running",
    )
    ctx = LaunchContext(
        session=session,
        worktree=Path(worktree),
        job_path=Path(worktree) / "FOREMAN-JOB.md",
        role_path=Path(worktree) / "FOREMAN-ROLE.md",
        log_path=Path(session.log),
        pid_path=paths.session_pid_path(new_id),
        verdict_path=paths.session_verdict_path(new_id),
        scratch_dir=paths.session_scratch_dir(new_id),
        branch=f"foreman/{new_id}",
        target="main",
        timeout=session.timeout or "20m",
    )
    try:
        paths.session_dir(new_id).mkdir(parents=True, exist_ok=True)
        Path(session.log).touch(exist_ok=True)
        try:
            checkpoint = Path(paths.checkpoint_path(sid)).read_text(
                encoding="utf-8")
            Path(paths.checkpoint_path(new_id)).write_text(
                checkpoint, encoding="utf-8")
        except OSError:
            pass
        adapter = get_pool(session.pool)
        pid = adapter.launch(ctx)
    except Exception:  # noqa: BLE001 - a dead spawn stays a dead supervisor
        return None
    launched = Session(
        id=new_id,
        role=session.role,
        pool=session.pool,
        model=session.model,
        component=session.component,
        pid=pid,
        pgid=pid,
        worktree=session.worktree,
        log=session.log,
        timeout=session.timeout,
        launched_by=sid,
        started_at=now_iso,
        state="running",
    )

    def place(roster):
        if not isinstance(roster, dict):
            roster = {"sessions": {}}
        sessions = roster.get("sessions")
        if not isinstance(sessions, dict):
            sessions = roster["sessions"] = {}
        sessions[new_id] = launched.to_dict()
        old = sessions.get(sid)
        if isinstance(old, dict):
            old["state"] = "exited"
        return roster

    store.update_snapshot(paths.roster_path(), place,
                          default={"sessions": {}})
    _release_slots(sid, now_iso)
    cstate["relaunches"].append({"from": sid, "to": new_id, "at": now_iso})
    cstate["parents"][new_id] = sid
    return new_id


def tick(now: datetime | None = None,
         config: CollectorConfig | None = None) -> dict:
    """Run one observation tick. Returns the observed.json payload."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    now_iso = moment.isoformat()
    config = config or load_config()

    from .pools import get as get_pool

    table = procs.snapshot()
    roster = store.read_snapshot(paths.roster_path(),
                                 default={"sessions": {}})
    if not isinstance(roster, dict):
        roster = {"sessions": {}}
    sessions = roster.get("sessions")
    if not isinstance(sessions, dict):
        sessions = roster["sessions"] = {}

    open_now = _open_map(store.read_ledger(paths.anomalies_path()))
    cstate = _load_state()
    baselines = cstate["sessions"]

    def note_open(kind: str, subject: str, detail: str,
                 asserted: set) -> None:
        asserted.add((kind, subject))
        if (kind, subject) not in open_now:
            entry = store.append_ledger(
                paths.anomalies_path(),
                _anomaly_line(kind, subject, now_iso, detail))
            open_now[(kind, subject)] = entry

    asserted: set[tuple[str, str]] = set()
    roster_updates: dict[str, dict] = {}
    session_view: dict[str, dict] = {}
    alive_count = 0

    trees: dict[str, set[int]] = {}
    for sid, record in sessions.items():
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        pid = pid if isinstance(pid, int) and pid > 0 else None
        trees[sid] = procs.descendants(pid, table) if (
            pid is not None and table) else ({pid} if (
                pid is not None and procs.pid_alive(pid)) else set())

    for sid, record in sessions.items():
        if not isinstance(record, dict):
            continue
        role = record.get("role") or ""
        worker = role not in WORKER_EXEMPT_ROLES
        pid = record.get("pid")
        pid = pid if isinstance(pid, int) and pid > 0 else None
        alive = pid in trees[sid] if table else procs.pid_alive(pid)
        if alive:
            alive_count += 1

        observed: dict | None = None
        if pid is not None:
            try:
                adapter = get_pool(record.get("pool") or "")
            except ValueError:
                adapter = None
            if adapter is not None:
                try:
                    observed = adapter.observe(Session.from_dict(record))
                except Exception:  # noqa: BLE001 - a broken adapter
                    observed = None  # must not take the daemon down
        mtime = observed.get("transcript_mtime") if observed else None
        mtime = mtime if isinstance(mtime, (int, float)) else None
        cpu = observed.get("cpu_s") if observed else None
        cpu = float(cpu) if isinstance(cpu, (int, float)) else None
        finish = bool(observed.get("finish_present")) if observed else False

        update: dict = {}
        if cpu is not None:
            update["cpu_s"] = cpu
        if alive:
            update["last_observed_at"] = now_iso

        baseline = baselines.get(sid)
        baseline = baseline if isinstance(baseline, dict) else None
        if alive and observed is not None:
            if baseline is None:
                active_at = record.get("started_at") or now_iso
                if _parse_time(active_at) is None:
                    active_at = now_iso
                baselines[sid] = {"cpu": cpu, "mtime": mtime,
                                  "active_at": active_at}
            elif cpu != baseline.get("cpu") or mtime != baseline.get("mtime"):
                baseline["cpu"], baseline["mtime"] = cpu, mtime
                baseline["active_at"] = now_iso
        elif alive and baseline is None:
            # No observation source: never let "unknown" read as "idle".
            baselines[sid] = {"cpu": cpu, "mtime": mtime,
                              "active_at": now_iso}
        active_at = (baselines.get(sid) or {}).get("active_at")
        if _parse_time(active_at) is None:
            active_at = None

        state = record.get("state")
        started = _parse_time(record.get("started_at"))
        timeout_s = parse_duration(record.get("timeout"))
        elapsed = (moment - started).total_seconds() if started else None

        if role == "supervisor" and pid is not None and not alive:
            if state in RUNNING_LIKE:
                if _relaunch_count(sid, cstate, moment,
                                   config.relaunch_window_seconds) >= \
                        config.relaunch_limit:
                    note_open("supervisor dead", sid,
                              f"supervisor {sid} dead after "
                              f"{config.relaunch_limit} relaunches within "
                              f"the hour; left dead", asserted)
                    update["state"] = "exited"
                else:
                    new_id = _relaunch_supervisor(sid, record, now_iso,
                                                  cstate, config)
                    if new_id is None:
                        note_open("supervisor dead", sid,
                                  f"supervisor {sid} dead; relaunch failed",
                                  asserted)
                        update["state"] = "exited"
                    # A successful relaunch moves the old session to exited
                    # inside _relaunch_supervisor's own roster write.
            elif state not in ("exited", "killed"):
                update["state"] = "exited"
        elif role == "supervisor" and alive:
            declared = _parse_time(record.get("last_declared_at")) or started
            running_jobs = any(
                other is not record
                and isinstance(other, dict)
                and (other.get("role") or "") not in WORKER_EXEMPT_ROLES
                and (record.get("component") is None
                     or other.get("component") == record.get("component"))
                and (other.get("pid") in trees.get(other_sid, set())
                     if table else procs.pid_alive(other.get("pid"))
                     if isinstance(other.get("pid"), int) else False)
                for other_sid, other in sessions.items()
            )
            if declared is not None and not running_jobs and (
                    moment - declared).total_seconds() > \
                    config.supervisor_silent_seconds:
                note_open("supervisor silent", sid,
                          f"supervisor {sid} wrote nothing for "
                          f"{config.supervisor_silent_seconds:.0f}s and runs "
                          f"no jobs", asserted)
        elif worker and pid is not None and not alive:
            if finish:
                _mark_job(record.get("component"), record.get("job"),
                          "returned", now_iso)
            if state in RUNNING_LIKE:
                update["state"] = "exited"
        elif worker and alive and not finish:
            if elapsed is not None and timeout_s is not None and \
                    elapsed > timeout_s:
                procs.kill_tree(pid, table)
                update["state"] = "killed"
                _mark_job(record.get("component"), record.get("job"),
                          "failed", None)
                job = record.get("job") or "(no job)"
                note_open("job timeout", sid,
                          f"elapsed {elapsed:.0f}s past timeout "
                          f"'{record.get('timeout')}' (job {job}); killed",
                          asserted)
            elif active_at is not None and (
                    moment - _parse_time(active_at)).total_seconds() > \
                    config.job_stalled_seconds:
                note_open("job stalled", sid,
                          f"no worktree write and no cpu for "
                          f"{config.job_stalled_seconds:.0f}s", asserted)
                if state == "running":
                    update["state"] = "stalled"
            elif state == "stalled":
                update["state"] = "running"
        elif worker and alive and finish:
            children = {member for member in trees[sid] if member != pid} \
                if pid is not None else set()
            if children:
                note_open("job tail", sid,
                          f"finish marker present but {len(children)} "
                          f"process(es) still alive", asserted)
            elif state in RUNNING_LIKE:
                update["state"] = "exited"
                _mark_job(record.get("component"), record.get("job"),
                          "returned", now_iso)

        if update:
            roster_updates[sid] = update
        entry: dict = {"state": update.get("state", state),
                       "alive": alive}
        if cpu is not None:
            entry["cpu_s"] = cpu
        declared_at = _parse_time(record.get("last_declared_at"))
        if declared_at is not None:
            entry["seconds_since_declared"] = (
                moment - declared_at).total_seconds()
        if active_at is not None:
            entry["seconds_since_activity"] = (
                moment - _parse_time(active_at)).total_seconds()
        session_view[sid] = entry

    # Intruders: vendor processes no roster session claims.
    for pid, info in table.items():
        if info["state"] == "Z" or pid == os.getpid():
            continue
        cmdline = info["cmdline"]
        if not cmdline or not any(m in cmdline
                                  for m in config.vendor_markers):
            continue
        if any(pid in members for members in trees.values()):
            continue
        note_open("intruder", str(pid),
                  f"unclaimed vendor process {pid}: {cmdline[:200]}",
                  asserted)

    # Unregistered writers: ledger lines whose `by` the roster never minted.
    ledger_files: list[tuple[str, Path]] = [
        (name, paths.state_dir() / name) for name in SCAN_LEDGERS]
    try:
        components = sorted(Path(paths.components_dir()).iterdir())
    except OSError:
        components = []
    for component in components:
        for name in JOB_LEDGER_NAMES:
            ledger_files.append((f"{component.name}/{name}",
                                 component / name))
    for label, path in ledger_files:
        try:
            records = store.read_ledger(path)
        except OSError:
            continue
        for record in records:
            by = record.get("by")
            if not isinstance(by, str) or not by or by == OWNER:
                continue
            if by not in sessions:
                note_open("unregistered writer", by,
                          f"ledger line in {label} claims unknown "
                          f"session '{by}'", asserted)

    # Resolve what this tick no longer asserts. Unregistered-writer lines
    # clear only when the roster learns the id: the ledger line itself is
    # append-only, so absence from this tick's scan proves nothing.
    for (kind, subject), record in open_now.items():
        if (kind, subject) in asserted:
            continue
        if kind == "unregistered writer":
            if subject in sessions:
                store.append_ledger(paths.anomalies_path(),
                                    dict(record, resolved_at=now_iso))
            continue
        if kind in REASSERT_KINDS:
            store.append_ledger(paths.anomalies_path(),
                                dict(record, resolved_at=now_iso))

    if roster_updates:
        def apply(roster):
            if not isinstance(roster, dict):
                return roster
            current = roster.get("sessions")
            if not isinstance(current, dict):
                return roster
            for sid, changes in roster_updates.items():
                entry = current.get(sid)
                if isinstance(entry, dict):
                    entry.update(changes)
            return roster

        store.update_snapshot(paths.roster_path(), apply,
                              default={"sessions": {}})

    # Baselines for sessions that left the roster would only grow.
    for sid in list(baselines):
        if sid not in sessions:
            del baselines[sid]
    window = config.relaunch_window_seconds
    cstate["relaunches"] = [
        event for event in cstate["relaunches"]
        if isinstance(event, dict) and (
            _parse_time(event.get("at")) is None or
            (moment - _parse_time(event.get("at"))).total_seconds() <= window)]
    store.write_snapshot(paths.collector_path(), cstate)

    observed_payload = {
        "at": now_iso,
        "sessions": session_view,
        "jobs": _jobs_view(moment),
        "pools": _pools_view(config),
        "swarm": _swarm_view(moment, len(sessions), alive_count),
    }
    store.write_snapshot(paths.observed_path(), observed_payload)
    return observed_payload


def _jobs_view(moment: datetime) -> dict:
    from .pools import get as _get

    folded = read_jobs()
    roster = store.read_snapshot(paths.roster_path(), default={"sessions": {}})
    by_job: dict[str, dict] = {}
    if isinstance(roster, dict):
        for record in (roster.get("sessions") or {}).values():
            if isinstance(record, dict) and record.get("job"):
                by_job.setdefault(str(record["job"]), record)
    view = {}
    for jid, (component, record) in folded.items():
        entry: dict = {"state": record.get("state"),
                       "component": component}
        started = _parse_time(record.get("started_at"))
        if started is not None:
            entry["elapsed_s"] = (moment - started).total_seconds()
        timeout_s = parse_duration(record.get("timeout"))
        if timeout_s is not None:
            entry["timeout_s"] = timeout_s
        moment_mtime = _worktree_mtime(record.get("worktree"))
        if moment_mtime is None and jid in by_job:
            try:
                adapter = _get((by_job[jid].get("pool") or ""))
            except ValueError:
                adapter = None
            if adapter is not None:
                try:
                    moment_mtime = adapter.observe(
                        Session.from_dict(by_job[jid])).get(
                            "transcript_mtime")
                except Exception:  # noqa: BLE001 - numbers stay absent
                    moment_mtime = None
        if isinstance(moment_mtime, (int, float)):
            entry["minutes_since_write"] = (moment.timestamp()
                                            - moment_mtime) / 60
        view[jid] = entry
    return view


def _pools_view(config: CollectorConfig) -> dict:
    try:
        grants = store.read_ledger(paths.slots_path())
    except OSError:
        grants = []
    folded: dict[tuple, dict] = {}
    for record in grants:
        folded[(record.get("pool"), record.get("session"),
                record.get("job"), record.get("granted_at"))] = record
    held: dict[str, int] = {}
    pools: set[str] = set(config.pools_total)
    for (pool, _session, _job, _at), record in folded.items():
        if not isinstance(pool, str) or not pool:
            continue
        pools.add(pool)
        if record.get("released_at") is None:
            held[pool] = held.get(pool, 0) + 1
    roster = store.read_snapshot(paths.roster_path(), default={"sessions": {}})
    if isinstance(roster, dict):
        for record in (roster.get("sessions") or {}).values():
            if isinstance(record, dict) and record.get("pool"):
                pools.add(record["pool"])
    view = {}
    for pool in sorted(pools):
        entry: dict = {"held": held.get(pool, 0)}
        if pool in config.pools_total:
            entry["total"] = config.pools_total[pool]
        view[pool] = entry
    return view


def _swarm_view(moment: datetime, registered: int, observed: int) -> dict:
    try:
        inbox = _fold_by_id(store.read_ledger(paths.inbox_path()))
    except OSError:
        inbox = {}
    open_items = [item for item in inbox.values()
                  if item.get("answered_at") is None]
    oldest: float | None = None
    for item in open_items:
        asked = _parse_time(item.get("asked_at"))
        if asked is not None:
            age = (moment - asked).total_seconds()
            oldest = age if oldest is None or age > oldest else oldest
    open_anomalies = len(_open_map(store.read_ledger(paths.anomalies_path())))
    view: dict = {"sessions_registered": registered,
                  "sessions_observed": observed,
                  "inbox_depth": len(open_items),
                  "open_anomalies": open_anomalies}
    if oldest is not None:
        view["inbox_oldest_s"] = oldest
    return view


#: Fallback when the checkout's packaging/ directory is not around
#: (an installed package); kept identical to the template file.
UNIT_TEMPLATE = """\
# Foreman collector daemon, systemd user unit template.
# Placeholders (@NAME@) are filled in by `foreman collector unit`, which
# prints this file with the real paths: never put a home directory here,
# this repository is public.
# Install: mkdir -p ~/.config/systemd/user && foreman collector unit >
#   ~/.config/systemd/user/foreman-collector.service, then
#   systemctl --user daemon-reload && systemctl --user enable --now foreman-collector
[Unit]
Description=Foreman collector daemon (observe, flag, relaunch)
After=network.target

[Service]
Type=simple
ExecStart=@FOREMAN_BIN@ collector run
Environment=FOREMAN_STATE=@STATE_DIR@
Environment=FOREMAN_CONFIG=@CONFIG_DIR@
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _unit_template() -> str:
    packaged = Path(__file__).resolve().parent.parent.parent / \
        "packaging" / "foreman-collector.service"
    try:
        return packaged.read_text(encoding="utf-8")
    except OSError:
        return UNIT_TEMPLATE


def render_unit() -> str:
    """The unit template with this machine's real paths filled in."""
    binary = shutil.which("foreman")
    start = binary if binary else f"{sys.executable} -m foreman"
    return (_unit_template()
            .replace("@FOREMAN_BIN@", start)
            .replace("@STATE_DIR@", str(paths.state_dir()))
            .replace("@CONFIG_DIR@", str(paths.config_dir())))


def collector_main(action: str) -> int:
    if action == "unit":
        print(render_unit(), end="")
        return 0
    if action == "once":
        tick()
        return 0
    config = load_config()
    while True:
        try:
            tick(config=config)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - the daemon stays up
            print(f"foreman collector: tick failed: {exc}", file=sys.stderr)
        try:
            time.sleep(config.tick_seconds)
        except KeyboardInterrupt:
            return 0
    return 0


def add_collector_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("run", "once", "unit"),
                        help="run the daemon, run one tick, or print the "
                             "systemd user unit")


@subcommand("collector", help="Observe the swarm, flag anomalies, relaunch.")
def _collector_entry(args: argparse.Namespace) -> int:
    try:
        return collector_main(args.action)
    except KeyboardInterrupt:
        return 0


_collector_entry.add_arguments = add_collector_arguments  # type: ignore[attr-defined]
