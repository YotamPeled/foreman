"""The collector daemon: observe, compute, flag.

One tick, every two seconds: read the process table, ask each live
session's pool adapter for cpu seconds and finish marker, read each
worktree mtime, write the observed fields back onto the roster snapshot,
and recompute ``observed.json`` (docs/DESIGN.md section 7, only the
numbers v0 has a source for). Then the seven anomalies of this version
(section 10): supervisor silent, job stalled, job timeout (kill),
job tail, intruder, unregistered writer, collector stale. A dead
supervisor is summoned anew through the same ``relaunch`` verb the owner
calls, up to the configured limit inside the window; past that it is
flagged for a person.

The collector refuses nothing and grants nothing: it observes,
records, kills on timeout and summons a dead supervisor anew.

Every threshold comes from ``foreman.toml``; ``DEFAULTS`` below holds
the design's values and is the only place they appear. Clocks are
injected (``tick(now=...)``) so tests never wait on them.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import capacity, paths, procs, store
from .caller import OWNER, SESSION_ENV
from .cli import subcommand
from .entities import Session

#: The design's values, in one place. Call sites read the config object.
DEFAULTS = {
    "tick_seconds": 2,
    "supervisor_silent_seconds": 15 * 60,
    "job_stalled_seconds": 10 * 60,
    "relaunch_limit": 2,
    "relaunch_window_seconds": 60 * 60,
    "vendor_markers": ("muse", "grok", "claude"),
    "heartbeat_minutes": 20,
    "turn_stale_seconds": 10 * 60,
}

WORKER_EXEMPT_ROLES = ("supervisor", "foreman", "owner")
RUNNING_LIKE = ("running", "starting", "stalled")
JOB_RUNNING_LIKE = ("planned", "queued", "running")
TERMINAL_JOB_STATES = ("returned", "returned-with-work", "verified",
                        "failed", "killed", "history")
TERMINAL_SESSION_STATES = ("exited", "killed")
REASSERT_KINDS = (
    "supervisor silent",
    "supervisor dead",
    "job stalled",
    "job timeout",
    "job tail",
    "intruder",
    "collector stale",
    "monitor stale",
    "monitor alert",
)

#: Opened when the checkout moved under a running collector: the screen it
#: writes can be silently wrong, which weighs the same as a silent
#: supervisor. The subject is the collector itself.
COLLECTOR_STALE_KIND = "collector stale"
COLLECTOR_SUBJECT = "collector"

#: The systemd user unit the collector runs as where one is installed.
COLLECTOR_UNIT = "foreman-collector.service"

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
    heartbeat_minutes: float = DEFAULTS["heartbeat_minutes"]
    turn_stale_seconds: float = DEFAULTS["turn_stale_seconds"]
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
                    "job_stalled_seconds", "relaunch_window_seconds",
                    "heartbeat_minutes", "turn_stale_seconds"):
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
    # `[pool.<name>].cap` is the owner's word on how much a pool may hold
    # (see :mod:`foreman.config`), so observed.json carries the same number
    # the launcher refuses against; `[pools.<name>].slots_total` is the
    # older spelling and still wins where both are written.
    for table_name in ("pool", "pools"):
        pools = raw.get(table_name)
        if not isinstance(pools, dict):
            continue
        for name, entry in pools.items():
            if not isinstance(entry, dict):
                continue
            for key in ("cap", "slots_total", "slots"):
                total = entry.get(key)
                if isinstance(total, int) and not isinstance(total, bool) \
                        and total >= 0:
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
    """Every job ledger line, folded last-wins: id -> (front, record)."""
    folded: dict[str, tuple[str, dict]] = {}
    try:
        fronts = sorted(Path(paths.fronts_dir()).iterdir())
    except OSError:
        return folded
    for front in fronts:
        try:
            records = store.read_ledger(front / "jobs.jsonl")
        except OSError:
            continue
        for record in records:
            jid = record.get("id")
            if isinstance(jid, str) and jid:
                folded[jid] = (front.name, record)
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


def _package_dir() -> Path:
    """The directory holding this package's source files."""
    return Path(__file__).resolve().parent


def _checkout_root() -> Path:
    """The checkout this module was imported from (``src/`` layout)."""
    return Path(__file__).resolve().parent.parent.parent


def _git_head(checkout: Path | None = None) -> str | None:
    """The checkout's git head, or None where there is none to record.

    A missing ``git``, a directory that is not a checkout and an installed
    wheel all degrade to None, never to an error: there is no head for the
    collector to be stale against.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout or _checkout_root()),
             "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _source_mtime(package: Path | None = None) -> float | None:
    """Newest mtime across the package's source files, None if unreadable."""
    newest: float | None = None
    try:
        for path in (package or _package_dir()).rglob("*.py"):
            try:
                moment = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or moment > newest:
                newest = moment
    except OSError:
        return None
    return newest


def _check_collector_staleness(cstate: dict, now_iso: str,
                               note_open, asserted: set) -> None:
    """Open ``collector stale`` when the checkout moved under this process.

    The record in collector.json is what this process started with: a fresh
    collector baselines it and never flags, while a running one flags when
    the head moved or a source file is newer than the recorded mtime. A
    stale tick keeps the old record so the line stays open until a restart
    re-records; a fresh tick adopts the current values.
    """
    recorded_head = cstate.get("code_head")
    recorded_mtime = cstate.get("code_mtime")
    current_head = _git_head()
    current_mtime = _source_mtime()
    head_stale = (
        isinstance(recorded_head, str) and bool(recorded_head)
        and isinstance(current_head, str) and bool(current_head)
        and current_head != recorded_head)
    mtime_stale = (
        isinstance(recorded_mtime, (int, float))
        and isinstance(current_mtime, (int, float))
        and current_mtime > recorded_mtime)
    if head_stale or mtime_stale:
        note_open(
            COLLECTOR_STALE_KIND, COLLECTOR_SUBJECT,
            f"collector stale since {now_iso} — foreman collector restart",
            asserted)
        return
    if "code_head" not in cstate or current_head is not None:
        cstate["code_head"] = current_head
    if "code_mtime" not in cstate or current_mtime is not None:
        cstate["code_mtime"] = current_mtime


def record_startup_version() -> None:
    """Record the checkout this process started from.

    The daemon calls this once at startup, never on a tick: a restarted
    collector therefore starts fresh by construction, and the tick's resolve
    pass closes the old ``collector stale`` line with nothing to close by
    hand. Reading the head costs about a millisecond and the source scan
    far less, so neither needs a cache.

    ``started_at`` is the stamp pool verbs compare against config mtimes.
    A tick never rewrites it: an older collector.json without one stays
    without one, and a running collector keeps the instant it started.
    """
    state = _load_state()
    state["code_head"] = _git_head()
    state["code_mtime"] = _source_mtime()
    state["started_at"] = store.utcnow_iso()
    store.write_snapshot(paths.collector_path(), state)


def _load_state() -> dict:
    state = store.read_snapshot(paths.collector_path(), default=None)
    if not isinstance(state, dict):
        return {"sessions": {}, "relaunches": [], "parents": {},
                "turn_attempts": {}, "queue_starts": []}
    for key, default in (("sessions", {}), ("relaunches", []),
                         ("parents", {}), ("turn_attempts", {}),
                         ("queue_starts", [])):
        if not isinstance(state.get(key), (dict, list)):
            state[key] = default
    if not isinstance(state["sessions"], dict):
        state["sessions"] = {}
    if not isinstance(state["relaunches"], list):
        state["relaunches"] = []
    if not isinstance(state["parents"], dict):
        state["parents"] = {}
    if not isinstance(state.get("queue_starts"), list):
        state["queue_starts"] = []
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


def _relaunch_dead_supervisor(sid: str, record: dict, cstate: dict,
                              moment: datetime, now_iso: str,
                              config: CollectorConfig) -> str | None:
    """Summon a dead supervisor anew. None when it worked, else why not.

    The daemon calls the same ``relaunch`` the owner calls: one path
    summons a supervisor, so the prompt an automatic relaunch delivers is
    the prompt a hand-typed one delivers, and a defect in either is a
    defect in both. What is the collector's own is the judgement of when:
    a front to belong to, no freeze, and no more than ``relaunch_limit``
    relaunches of this session inside the window. Past that the anomaly
    says so and a person decides — a supervisor that dies on every summon
    would otherwise be summoned forever.
    """
    front = record.get("front")
    if not front:
        return (f"supervisor {sid} dead and belongs to no front; a person "
                f"must relaunch it from its checkpoint")
    if paths.frozen_path().exists():
        return (f"supervisor {sid} dead while the swarm is frozen; nothing "
                f"is summoned until the freeze is lifted")
    count = _relaunch_count(sid, cstate, moment,
                            config.relaunch_window_seconds)
    if count >= config.relaunch_limit:
        return (f"supervisor {sid} dead after {count} automatic relaunch(es) "
                f"in the last {config.relaunch_window_seconds / 60:.0f} "
                f"minutes; a person must relaunch it from its checkpoint")
    from . import launch as launch_module

    try:
        code = launch_module.relaunch_main(sid, by=COLLECTOR_SUBJECT,
                                           quiet=True)
        detail = "the launcher refused it"
    except Exception as exc:  # noqa: BLE001 - a failed relaunch is a line,
        code, detail = 1, f"{type(exc).__name__}: {exc}"  # never a dead daemon
    if code != 0:
        return (f"supervisor {sid} dead and the automatic relaunch failed "
                f"({detail}); a person must relaunch it from its checkpoint")
    cstate["relaunches"].append({"from": sid, "at": now_iso})
    return None


def _release_slots(session_id: str, now_iso: str, because: str) -> None:
    """Release every still-open slot grant for ``session_id``.

    Called on death, whatever happens next: a grant is held by a live
    session, so a dead, finished or killed session must not keep holding
    one. This is the moment a slot comes back, and it is why the collector
    is where the killing lives: a job the screen shows as running an hour
    after somebody stopped it is exactly the lie this daemon exists to
    prevent. Folded last-wins, so an already-released grant is not
    released twice.
    """
    capacity.release_for_session(session_id, now_iso, because)


def _stopped_by(record: dict) -> str | None:
    """The session that stopped this one, where a Foreman verb did it.

    A verb that stops a session writes its own id onto the roster record;
    a process that merely vanished leaves nothing, and then the job record
    says only that it went, naming nobody.
    """
    who = record.get("killed_by")
    return who if isinstance(who, str) and who else None


def _note_pool_refusal(adapter, record: dict, sid: str) -> None:
    """If this dead worker's transcript is a quota refusal, append one
    pools.jsonl record before the job is marked. A second tick over the
    same job writes nothing."""
    if adapter is None:
        return
    job = record.get("job")
    try:
        existing = store.read_ledger(paths.pools_path())
    except OSError:
        existing = []
    for line in existing:
        if not isinstance(line, dict):
            continue
        if job and line.get("job") == job:
            return
        if not job and line.get("session") == sid:
            return
    try:
        refusal = adapter.refusal(Session.from_dict(record))
    except Exception:  # noqa: BLE001 - a broken adapter must not
        return          # take the daemon down
    if not isinstance(refusal, dict):
        return
    reset = refusal.get("reset")
    if not isinstance(reset, str) or not reset:
        return
    try:
        datetime.fromisoformat(reset.replace("Z", "+00:00"))
    except ValueError:
        return
    pool = record.get("pool") or getattr(adapter, "name", "") or ""
    if not isinstance(pool, str) or not pool:
        return
    kind = refusal.get("kind")
    because = kind if isinstance(kind, str) and kind else "quota"
    detail = refusal.get("detail")
    detail = " ".join(detail.split()) if isinstance(detail, str) else ""
    store.append_ledger(paths.pools_path(), {
        "id": pool,
        "pool": pool,
        "out_until": reset,
        "because": because,
        "detail": detail,
        "job": job,
        "session": sid,
    }, session_id=sid)


def _exit_code_of(observed: dict | None, finish: bool) -> int | None:
    """The worker's exit code when a finish marker was read, else None."""
    if not finish or not isinstance(observed, dict):
        return None
    code = observed.get("finish_rc")
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _job_head(record: dict) -> str:
    """The sha of the job line's branch in its worktree, or "".

    ``git -C <worktree> rev-parse --verify <branch>`` is the answer.
    Anything unreadable — no branch, a gone worktree, git failing —
    answers empty, so a tick never blocks on a missing checkout.
    """
    branch = (record.get("branch") or "").strip() \
        if isinstance(record.get("branch"), str) else ""
    if not branch:
        return ""
    repo = record.get("worktree") \
        if isinstance(record.get("worktree"), str) else ""
    if not repo or not os.path.isdir(repo):
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", branch],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _mark_job(front: str | None, job_id: str | None, to_state: str,
              stamp: str | None, by: str | None = None,
              reason: str | None = None,
              exit_code: int | None = None) -> None:
    """Move a job to ``to_state``. A terminal state is terminal: when the
    latest record for the job is already returned, returned-with-work,
    verified, failed, killed or history, nothing is appended, so a
    returned event is never duplicated and a verification or failure is
    never overwritten by a later tick. The one append carries the one wake event for the
    job's launcher: the transition is computed here, so the event is
    emitted here rather than recomputed anywhere else. ``reason``
    overrides the event's reason where the cause differs from the state
    (a timeout kill marks the job failed but wakes ``job timed out``).
    ``exit_code`` is the worker's code from a finish marker, or None
    when no marker was read; ``reason``, when given, is stored as
    ``outcome_reason`` on the job line."""
    if not front or not job_id:
        return
    try:
        records = store.read_ledger(
            paths.front_jobs_path(front))
    except OSError:
        return
    latest: dict | None = None
    for record in records:
        if record.get("id") == job_id:
            latest = record
    if latest is None or latest.get("state") in TERMINAL_JOB_STATES:
        return
    if latest.get("state") not in JOB_RUNNING_LIKE:
        return
    revised = dict(latest, state=to_state, exit_code=exit_code)
    try:
        revised["head"] = _job_head(latest)
    except Exception:  # noqa: BLE001 - a head lookup never blocks a tick
        revised["head"] = ""
    if reason:
        revised["outcome_reason"] = reason
    if stamp is not None and to_state in ("returned", "returned-with-work"):
        revised["returned_at"] = stamp
    if to_state == "killed":
        # A killed line is authored by whoever stopped the job, and by
        # nobody where the process merely vanished. Carrying the `by` of
        # the line it revises would name the launcher as the killer.
        revised.pop("by", None)
    store.append_ledger(paths.front_jobs_path(front), revised, session_id=by)
    from . import wake as _wake

    _wake.emit_job_event(front, job_id, revised,
                         reason or f"job {to_state}", stamp)


def _latest_job_record(front: str | None,
                        job_id: str | None) -> dict | None:
    """The latest ledger line for a job, or None where there is none."""
    if not front or not job_id:
        return None
    try:
        records = store.read_ledger(paths.front_jobs_path(front))
    except OSError:
        return None
    latest: dict | None = None
    for record in records:
        if record.get("id") == job_id:
            latest = record
    return latest


def _job_branch_moved(front: str | None, job_id: str | None) -> bool:
    """True when the job's branch holds commits past its base.

    The branch-moved check lives in :mod:`foreman.progress` beside the
    verify that relies on it; it is imported here lazily so the two
    modules stay import-independent. Anything unreadable answers False.
    """
    try:
        from .progress import job_branch_moved
    except ImportError:  # pragma: no cover - the module is always present
        return False
    record = _latest_job_record(front, job_id)
    if record is None:
        return False
    try:
        return bool(job_branch_moved(record))
    except Exception:  # noqa: BLE001 - a branch check never blocks a tick
        return False


def _finish_rc_clean(observed: dict | None) -> bool:
    """True when the worker exited clean: a marker with rc 0, or a marker
    whose code the adapter did not report.

    A marker with a non-zero code is a crash that happened to write one,
    and reads as failed, never returned.
    """
    if not observed:
        return True
    code = observed.get("finish_rc")
    if code is None:
        return True
    return code == 0


def _tick_lock_path() -> Path:
    return paths.state_dir() / "collector.lock"


def _tree_of(pid: int | None, starttime, table: dict[int, dict]) -> set[int]:
    """The live tree of ``pid`` when its starttime still matches, else empty.

    A missing pid, a dead pid, or a starttime that does not match claims
    nothing: a recycled pid is not the session's.
    """
    if pid is None or not isinstance(pid, int) or pid <= 0:
        return set()
    if not procs.same_process(pid, starttime, table):
        return set()
    return procs.descendants(pid, table) if table else (
        {pid} if procs.pid_alive(pid) else set())


def _turn_tree(sid: str, table: dict[int, dict]) -> set[int]:
    """The process tree a live turn marker claims, or empty.

    The marker records the vendor the way a launch records a session:
    pid plus starttime. A stale marker — pid gone, or starttime not the
    process now at that pid — claims nothing.
    """
    marker = store.read_snapshot(paths.session_turn_path(sid), default=None)
    if not isinstance(marker, dict):
        return set()
    pid = marker.get("pid")
    pid = pid if isinstance(pid, int) and pid > 0 else None
    return _tree_of(pid, marker.get("pid_starttime"), table)


def _territory(sessions: dict) -> list[str]:
    """The directories a swarm process would be working in."""
    places = [str(paths.state_dir())]
    for record in sessions.values():
        if isinstance(record, dict) and record.get("worktree"):
            places.append(str(record["worktree"]))
    return places


def _inside(cwd: str | None, places: list[str]) -> bool:
    if not cwd:
        return False
    for place in places:
        try:
            if os.path.commonpath([os.path.realpath(cwd),
                                   os.path.realpath(place)]) == \
                    os.path.realpath(place):
                return True
        except (ValueError, OSError):
            continue
    return False


def _is_vendor_cmdline(cmdline: str, markers) -> bool:
    """True when the executable (not a substring) is a known vendor binary."""
    if not cmdline:
        return False
    executable = procs.executable_of(cmdline)
    return executable in set(markers)


def _tail_children(members: set[int], pid: int | None,
                   table: dict[int, dict]) -> set[int]:
    """Descendants that count as retries: everything but the root and the
    output-draining ``tee`` the wrapper pipeline leaves behind while it
    flushes the log after the finish marker."""
    out = set()
    for member in members:
        if pid is not None and member == pid:
            continue
        info = table.get(member, {})
        if procs.executable_of(info.get("cmdline", "")) == "tee":
            continue
        out.add(member)
    return out


def _declared_at(record: dict):
    """Latest declared write: the roster stamp (checkpoint verb) or the
    checkpoint file's own mtime, whichever is newer. Either alone can go
    stale; the newer of the two is what silence is measured against."""
    best = _parse_time(record.get("last_declared_at"))
    try:
        stamp = Path(paths.checkpoint_path(
            str(record.get("id") or ""))).stat().st_mtime
    except OSError:
        stamp = None
    if stamp is not None:
        from datetime import timezone as _tz
        file_time = datetime.fromtimestamp(stamp, tz=_tz.utc)
        if best is None or file_time > best:
            best = file_time
    return best


def _check_headless_silence(sid: str, record: dict, moment: datetime,
                            now_iso: str, config: CollectorConfig,
                            note_open, asserted: set) -> None:
    """``supervisor silent`` for a headless session, from wakes and turns.

    A headless session holds no window and no process between turns, so
    window activity and the process table say nothing about it. What says
    something is the two ledgers: a session that was woken and ran no
    turn never started working, and one that ran no turn and heard no
    wake for longer than the silence threshold never started either. A
    session between turns — turns on its ledger, whatever its queue holds —
    is working as designed and is never flagged: that false alarm is the
    one this check exists not to raise.
    """
    from . import headless as _headless
    from . import wake as _wake

    try:
        turns = _headless.read_turns(sid)
    except OSError:
        turns = []
    if turns:
        return
    events = _wake.read_events(sid)
    if events:
        last = _wake.last_wake(sid) or {}
        reason = last.get("reason") or "?"
        note_open("supervisor silent", sid,
                  f"supervisor {sid} woken (last wake '{reason}') "
                  f"but ran no turn", asserted)
        return
    base = _parse_time(record.get("started_at"))
    if base is None:
        return
    if (moment - base).total_seconds() > config.supervisor_silent_seconds:
        note_open("supervisor silent", sid,
                  f"supervisor {sid} ran no turn and had no wake for "
                  f"{config.supervisor_silent_seconds:.0f}s", asserted)


@contextmanager
def _exclusive_tick():
    """Hold an exclusive lock for one tick so overlapping ticks cannot
    duplicate a launch or open an anomaly twice."""
    path = _tick_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _live_supervisor_for(front: str | None, sessions: dict,
                           table: dict, trees: dict[str, set[int]]) -> bool:
    """True when a live supervisor for ``front`` is already running."""
    for other in sessions.values():
        if not isinstance(other, dict):
            continue
        if (other.get("role") or "") != "supervisor":
            continue
        if front is not None and other.get("front") != front:
            continue
        if other.get("state") not in RUNNING_LIKE:
            continue
        pid = other.get("pid")
        pid = pid if isinstance(pid, int) and pid > 0 else None
        if pid is None or pid not in trees.get(
                str(other.get("id") or ""), set()):
            continue
        if not procs.same_process(pid, other.get("pid_starttime"), table):
            continue
        return True
    return False


#: Prefix of the transient unit one spawned ``foreman turn`` runs as.
#: Per session *and* per attempt: a failed carrier stays loaded in its
#: failed state, and systemd refuses a new unit under a loaded name, so
#: reusing the name would wedge the clock behind one corpse.
TURN_CARRIER_UNIT_PREFIX = "foreman-turn-"


def turn_carrier_unit(session_id: str, attempt: int) -> str:
    """The transient unit one spawned turn carrier runs as.

    A different unit from the turn's own (``foreman-<session>``, the
    stable name ``foreman kill`` stops and ``headless.free_unit_name``
    clears before each turn): the carrier is the daemon's process, the
    turn is the session's, and only the per-attempt name keeps a failed
    carrier from blocking the next wake.
    """
    return f"{TURN_CARRIER_UNIT_PREFIX}{session_id}-{attempt}"


def turn_carrier_argv(session_id: str) -> list[str]:
    """``foreman turn <session>`` as this checkout's interpreter runs it.

    The interpreter path this process was started with, never a bare
    ``foreman`` off PATH: a collector running from a branch checkout
    carries the wake with that branch's verbs.

    Do not resolve the path. A virtual environment is the path, not the
    binary behind it: ``<venv>/bin/python3`` is usually a symlink to the
    system interpreter, and resolving it lands on an interpreter that
    cannot import this checkout's packages. ``sys.executable`` is
    already absolute; use it as it stands.
    """
    return [sys.executable, "-m", "foreman", "turn", session_id]


def turn_carrier_outer_argv(session_id: str, attempt: int) -> list[str]:
    """The detached spawn: the carrier under its per-attempt unit.

    ``systemd-run`` without ``--wait`` returns once the unit is started,
    so starting it is all the tick ever does with it. No working
    directory: the carrier reads absolute state paths, never a worktree.

    The world travels as ``--setenv=`` and not as the spawn's own
    environment. ``systemd-run --user`` hands the unit to the user
    manager, which starts it from *its* environment, not the caller's:
    an env= on the spawn reaches the systemd-run process and stops
    there. Measured, by a carrier that died on "No module named
    foreman" while its spawn's env held the right PYTHONPATH.
    """
    argv = ["systemd-run", "--user",
            f"--unit={turn_carrier_unit(session_id, attempt)}"]
    for name, value in sorted(turn_carrier_env(only_world=True).items()):
        argv.append(f"--setenv={name}={value}")
    return [*argv, *turn_carrier_argv(session_id)]


def turn_carrier_env(only_world: bool = False) -> dict[str, str]:
    """The world a turn carrier runs in: this process's, minus a session.

    The state and config directories travel the way every other launch
    carries them, and this checkout's ``src`` rides first on
    ``PYTHONPATH`` so the carrier runs this branch's verbs. The calling
    session does not travel: the carrier is the clock's own act, and it
    arrives as the owner, never as whatever summoned the daemon.
    """
    from . import mcp as mcp_module

    env = dict(os.environ)
    env.pop(SESSION_ENV, None)
    src = mcp_module.checkout_src()
    if src is not None:
        inherited = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(src) + (
            os.pathsep + inherited if inherited else "")
    if not only_world:
        return env
    # Just the part the unit must be told, since it inherits nothing:
    # which world to read and where the package is.
    world = {}
    for name in (paths.STATE_ENV, paths.CONFIG_ENV, "PYTHONPATH"):
        value = env.get(name)
        if value:
            world[name] = value
    return world


def _default_turn_spawn(argv: list[str], *,
                        env: dict[str, str]) -> Any:
    """Start one turn carrier detached and return at once.

    Nothing here waits on the carrier: a turn takes up to the turn
    timeout, and a daemon that blocked on one would stop observing the
    swarm. Detached the way every other fire-and-forget spawn in this
    runtime goes: no pipe, its own session, nothing inherited but the
    world it was handed.
    """
    return subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
        env=env)


def _carry_wakes(sessions: dict, cstate: dict) -> int:
    """Start one detached ``foreman turn`` per queued, uncarried wake.

    Every session with a queued wake and no running turn gets a carrier;
    a session already running a turn is skipped — ``wake.turn_running``
    is the check, and it is also what ``run_wake`` itself refuses on, so
    a double start cannot produce two turns — and a session with an empty
    queue is skipped. A skip touches no ledger byte: a held wake waits
    unstamped and in order for the first tick after the turn ends.
    Returns how many carriers went out.
    """
    from . import wake as _wake

    attempts = cstate.get("turn_attempts")
    if not isinstance(attempts, dict):
        attempts = cstate["turn_attempts"] = {}
    carried = 0
    for sid in sorted(sessions):
        if not isinstance(sid, str) or not sid:
            continue
        record = sessions.get(sid)
        if not isinstance(record, dict):
            continue
        if not record.get("headless"):
            # An interactive session is a conversation with a keyboard in
            # front of it. Carrying its wake would run `claude -p --resume`
            # inside that live conversation: two processes, one thread of
            # talk. A windowed session reads its own screen; only a
            # headless one has no other way to hear.
            continue
        if not _wake.pending_events(sid):
            continue
        if _wake.turn_running(sid):
            continue
        try:
            attempt = int(attempts.get(sid) or 0) + 1
        except (TypeError, ValueError):
            attempt = 1
        attempts[sid] = attempt
        try:
            _default_turn_spawn(turn_carrier_outer_argv(sid, attempt),
                                env=turn_carrier_env())
        except Exception:  # noqa: BLE001 - a failed spawn never stops
            continue  # the clock; the wake stays queued for the next tick
        carried += 1
    return carried


def _queued_front_names() -> list[str]:
    try:
        return sorted(entry.name for entry in Path(paths.fronts_dir()).iterdir()
                      if entry.is_dir())
    except OSError:
        return []


def _queue_tick(cstate: dict, now_iso: str) -> None:
    """Start the oldest startable queued job per front, rewrite waits.

    After observation and the anomaly passes. One model job per front
    goes through ``start_queued``. A ``script`` node with ``lands``
    whose wait is empty goes through ``landing.run`` (no slot); one
    script per repository per tick. Every other queued node gets a
    revise line only when its reason changed.
    """
    from . import fronts as fronts_mod
    from . import landing as landing_mod
    from . import launch as launch_module
    from . import node as node_mod
    from . import progress as progress_mod

    starts = cstate.get("queue_starts")
    if not isinstance(starts, list):
        starts = cstate["queue_starts"] = []
    started_repos: set[str] = set()
    for name in _queued_front_names():
        record = fronts_mod.read_front_record(name)
        if record is None:
            continue
        if str(record.get("state") or "") in ("done", "halted", "frozen"):
            continue
        folded, by_id = node_mod._read_nodes(name)
        queued = [node for node in folded
                  if node.get("kind") == "job"
                  and str(node.get("state") or "") == "queued"]
        if not queued:
            continue
        started = False
        for node in progress_mod._queue_order(queued):
            nid = str(node.get("id") or "")
            if not nid:
                continue
            waits = progress_mod.compute_node_waits(
                name, node, by_id, record)
            lands = str(node.get("lands") or "").strip()
            role = str(node.get("role") or "").strip()
            if role == "script" and lands and waits in ("", "ready"):
                built = by_id.get(lands) or node
                job = landing_mod._verified_job(name, built)
                repo = landing_mod._landing_repo(name, record, built, job)
                repo_key = os.path.realpath(repo) if repo else repo
                if repo_key in started_repos:
                    waits = "lock"
                else:
                    try:
                        result = landing_mod.run(
                            name, node, by=COLLECTOR_SUBJECT)
                    except Exception as exc:  # noqa: BLE001
                        waits = f"{type(exc).__name__}: {exc}"
                    else:
                        started_repos.add(repo_key)
                        starts.append({
                            "front": name, "node": nid,
                            "job": "", "at": now_iso,
                            "head": result.head, "ok": result.ok,
                        })
                        folded, by_id = node_mod._read_nodes(name)
                        continue
            if (not started and waits in ("", "ready")
                    and role != "script"):
                try:
                    job_id, err = launch_module.start_queued(
                        name, nid, by=COLLECTOR_SUBJECT)
                except Exception as exc:  # noqa: BLE001 - a failed start
                    job_id, err = None, (  # is a wait reason, not a dead tick
                        f"{type(exc).__name__}: {exc}")
                if job_id:
                    started = True
                    starts.append({"front": name, "node": nid,
                                   "job": job_id, "at": now_iso})
                    continue
                waits = err or "launcher refused"
            current = str(node.get("waits") or "")
            if current != waits:
                progress_mod._write_node_revise(
                    name, node, COLLECTOR_SUBJECT, "queue tick",
                    waits=waits)


def _tick_inner(moment: datetime, now_iso: str,
                config: CollectorConfig) -> dict:
    from .pools import _common as pool_common
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

    # Anomalies this tick opened (not merely reasserted): each fires one
    # on-alert hook after the state writes below are durable.
    opened: list[tuple[str, str, str]] = []

    def note_open(kind: str, subject: str, detail: str,
                  asserted: set) -> None:
        asserted.add((kind, subject))
        if (kind, subject) not in open_now:
            entry = store.append_ledger(
                paths.anomalies_path(),
                _anomaly_line(kind, subject, now_iso, detail))
            open_now[(kind, subject)] = entry
            opened.append((kind, subject, detail))

    asserted: set[tuple[str, str]] = set()
    _check_collector_staleness(cstate, now_iso, note_open, asserted)
    roster_updates: dict[str, dict] = {}
    session_view: dict[str, dict] = {}
    alive_count = 0

    trees: dict[str, set[int]] = {}
    for sid, record in sessions.items():
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        pid = pid if isinstance(pid, int) and pid > 0 else None
        claimed = _tree_of(pid, record.get("pid_starttime"), table)
        claimed |= _turn_tree(sid, table)
        trees[sid] = claimed

    for sid, record in sessions.items():
        if not isinstance(record, dict):
            continue
        role = record.get("role") or ""
        worker = role not in WORKER_EXEMPT_ROLES
        pid = record.get("pid")
        pid = pid if isinstance(pid, int) and pid > 0 else None
        identity_ok = procs.same_process(
            pid, record.get("pid_starttime"), table) if pid is not None \
            else False
        if table:
            alive = pid in trees[sid] if pid is not None else False
        else:
            alive = bool(procs.pid_alive(pid)) and identity_ok
        alive = bool(alive and identity_ok)
        if alive:
            alive_count += 1

        # Observe whatever the adapter can see, dead process or not: a
        # finish marker is a line in a log file and needs no live pid.
        # Gating observation on process identity meant a job that had
        # finished could never be marked returned, because by then the
        # pid was gone; identity gates believing a process is alive and
        # aiming a kill, nothing else.
        observed: dict | None = None
        try:
            adapter = get_pool(record.get("pool") or "")
        except ValueError:
            adapter = None
        if adapter is not None:
            try:
                observed = adapter.observe(Session.from_dict(record))
            except Exception:  # noqa: BLE001 - a broken adapter
                observed = None  # must not take the daemon down
        cpu = observed.get("cpu_s") if observed else None
        cpu = float(cpu) if isinstance(cpu, (int, float)) else None
        finish = bool(observed.get("finish_present")) if observed else False
        wmtime = _worktree_mtime(record.get("worktree"))

        update: dict = {}
        if cpu is not None:
            update["cpu_s"] = cpu
        if alive:
            update["last_observed_at"] = now_iso

        # Stall watches the worktree (not the transcript log: a job
        # repeating itself in its log looks busy) plus cpu burned. A first
        # observation establishes a baseline and never accuses; when there
        # is no observation source at all (no cpu and no worktree mtime)
        # active_at stays None so unknown can never age into a stall.
        baseline = baselines.get(sid)
        baseline = baseline if isinstance(baseline, dict) else None
        if alive:
            if baseline is None:
                if cpu is None and wmtime is None:
                    baselines[sid] = {"cpu": cpu, "wmtime": wmtime,
                                      "active_at": None}
                else:
                    baselines[sid] = {"cpu": cpu, "wmtime": wmtime,
                                      "active_at": now_iso}
            elif cpu is None and wmtime is None:
                baseline["cpu"], baseline["wmtime"] = cpu, wmtime
                baseline["active_at"] = None
            elif cpu != baseline.get("cpu") or \
                    wmtime != baseline.get("wmtime"):
                baseline["cpu"], baseline["wmtime"] = cpu, wmtime
                baseline["active_at"] = now_iso
        active_at = (baselines.get(sid) or {}).get("active_at")
        if _parse_time(active_at) is None:
            active_at = None

        state = record.get("state")
        terminal_session = state in TERMINAL_SESSION_STATES
        started = _parse_time(record.get("started_at"))
        timeout_s = parse_duration(record.get("timeout"))
        elapsed = (moment - started).total_seconds() if started else None
        overdue = elapsed is not None and timeout_s is not None and \
            elapsed > timeout_s

        if role == "supervisor" and record.get("headless") \
                and state in RUNNING_LIKE:
            # No window, no process between turns: liveness is wakes and
            # turns, never the process table below. A pid here would be a
            # stale breadcrumb, not a session, so this branch comes first.
            _check_headless_silence(sid, record, moment, now_iso, config,
                                    note_open, asserted)
        elif role == "supervisor" and pid is not None and not alive:
            # Re-check liveness immediately: the roster was read after the
            # process snapshot, so a supervisor registered since still reads
            # dead from the stale table. A live recheck never relaunches
            # (nothing relaunches) nor flags the alive as dead.
            if procs.pid_alive(pid) and procs.same_process(
                    pid, record.get("pid_starttime"), None):
                alive = True
                trees[sid] = {pid}
                alive_count += 1
            if alive:
                declared = _declared_at(record) or started
                if declared is not None and (
                        moment - declared).total_seconds() > \
                        config.supervisor_silent_seconds:
                    note_open("supervisor silent", sid,
                              f"supervisor {sid} wrote nothing for "
                              f"{config.supervisor_silent_seconds:.0f}s and runs "
                              f"no jobs", asserted)
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
                continue
            # Dead supervisor: summoned anew through the same `relaunch`
            # the owner calls, which is design flow 4 (section 14) working
            # unattended. Only a session the roster still calls live is
            # relaunched; a terminal record stays terminal.
            _release_slots(sid, now_iso, "failed")
            if state in RUNNING_LIKE:
                why = _relaunch_dead_supervisor(
                    sid, record, cstate, moment, now_iso, config)
                if why is None:
                    # The roster record is now the fresh process's own. Its
                    # state must not be written back to `exited` by this
                    # tick, and nothing observed of the dead process
                    # belongs on it, so this session leaves the tick here.
                    roster_updates.pop(sid, None)
                    session_view[sid] = {"state": "starting", "alive": True,
                                         "relaunched_at": now_iso}
                    continue
                note_open("supervisor dead", sid, why, asserted)
                update["state"] = "exited"
            elif not _live_supervisor_for(
                    record.get("front"), sessions, table, trees):
                note_open("supervisor dead", sid,
                          f"supervisor {sid} dead; a person must relaunch it "
                          f"from its checkpoint", asserted)
            # Terminal stays terminal: no other state change.
        elif role == "supervisor" and alive:
            declared = _declared_at(record) or started
            running_jobs = any(
                other is not record
                and isinstance(other, dict)
                and (other.get("role") or "") not in WORKER_EXEMPT_ROLES
                and other.get("state") in RUNNING_LIKE
                and (record.get("front") is None
                     or other.get("front") == record.get("front"))
                and isinstance(other.get("pid"), int)
                and other.get("pid") in trees.get(other_sid, set())
                and procs.same_process(
                    other.get("pid"), other.get("pid_starttime"), table)
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
            # The unit is the completion signal; the marker is the
            # fallback. Workers spawn without --collect, so a finished
            # unit still answers Result/ExecMainStatus here — and a unit
            # that says it failed failed even if it wrote a marker.
            _note_pool_refusal(adapter, record, sid)
            try:
                unit_failed = pool_common.unit_reports_failure(
                    pool_common.unit_status(sid))
            except Exception:  # noqa: BLE001 - status never blocks a tick
                unit_failed = False
            if unit_failed:
                _mark_job(record.get("front"), record.get("job"),
                          "failed", None,
                          exit_code=_exit_code_of(observed, finish))
                _release_slots(sid, now_iso, "failed")
            elif finish and _finish_rc_clean(observed):
                _mark_job(record.get("front"), record.get("job"),
                          "returned", now_iso,
                          exit_code=_exit_code_of(observed, finish))
                _release_slots(sid, now_iso, "returned")
            elif finish:
                # A finish marker with a non-zero code is a crash that
                # wrote a marker on its way down: failed, never returned.
                _mark_job(record.get("front"), record.get("job"),
                          "failed", None,
                          exit_code=_exit_code_of(observed, finish))
                _release_slots(sid, now_iso, "failed")
            elif _job_branch_moved(record.get("front"), record.get("job")):
                # The process is gone and the log carries no finish
                # marker, but the branch moved past its base: the work
                # exists, so the job returned with work of its own. A
                # state apart from failed, which a supervisor verifies
                # like any other return.
                _mark_job(record.get("front"), record.get("job"),
                          "returned-with-work", now_iso, exit_code=None)
                _release_slots(sid, now_iso, "returned")
            else:
                # The process is gone and the log carries no finish
                # marker, so the job did not return: it was stopped. It
                # becomes killed on this tick rather than staying running
                # forever, and the line names the session that stopped it
                # where a Foreman verb did and nobody where the process
                # merely vanished.
                _mark_job(record.get("front"), record.get("job"),
                          "killed", now_iso, by=_stopped_by(record),
                          exit_code=None)
                _release_slots(sid, now_iso, "killed")
            # The status above has been read, so the finished unit can be
            # forgotten on the collector's own schedule; without this every
            # finished unit lingers as failed. Best-effort and only while
            # the session is still around to own the unit.
            if unit_failed or state in RUNNING_LIKE:
                try:
                    pool_common.reset_failed_unit(sid)
                except Exception:  # noqa: BLE001 - cleanup never blocks
                    pass
            if state in RUNNING_LIKE:
                update["state"] = "exited"
        elif worker and alive and not terminal_session:
            if overdue:
                # The finish marker decides "returned", never "immortal":
                # an overdue tree dies marker or no marker. Signal the
                # launcher-recorded process group (not a stale descendant
                # snapshot) and only record killed when it is gone.
                pgid = record.get("pgid")
                pgid = pgid if isinstance(pgid, int) and pgid > 0 else pid
                _signalled, remaining = procs.kill_job(pid, pgid)
                job = record.get("job") or "(no job)"
                if not remaining:
                    update["state"] = "killed"
                    _mark_job(record.get("front"), record.get("job"),
                              "failed", None, reason="job timed out",
                              exit_code=_exit_code_of(observed, finish))
                    _release_slots(sid, now_iso, "killed")
                    note_open("job timeout", sid,
                              f"elapsed {elapsed:.0f}s past timeout "
                              f"'{record.get('timeout')}' (job {job}); killed",
                              asserted)
                else:
                    note_open("job timeout", sid,
                              f"elapsed {elapsed:.0f}s past timeout "
                              f"'{record.get('timeout')}' (job {job}); "
                              f"kill attempted, {len(remaining)} "
                              f"process(es) remain", asserted)
            elif finish:
                children = _tail_children(trees[sid], pid, table)
                if children:
                    note_open("job tail", sid,
                              f"finish marker present but {len(children)} "
                              f"process(es) still alive", asserted)
                elif state in RUNNING_LIKE:
                    update["state"] = "exited"
                    if _finish_rc_clean(observed):
                        _mark_job(record.get("front"), record.get("job"),
                                  "returned", now_iso,
                                  exit_code=_exit_code_of(observed, finish))
                        _release_slots(sid, now_iso, "returned")
                    else:
                        _mark_job(record.get("front"), record.get("job"),
                                  "failed", None,
                                  exit_code=_exit_code_of(observed, finish))
                        _release_slots(sid, now_iso, "failed")
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

    # Intruders: vendor processes working inside the swarm's own
    # territory that no roster session claims. The executable (first
    # cmdline token's basename) must be a shipped vendor binary — a
    # substring match would flag any command merely mentioning one — and
    # the process must be working inside a Foreman worktree or the state
    # directory. Every vendor process on the machine is not the swarm's
    # business: on 2026-09-08 the unscoped rule filled Problems with
    # sixteen lines, including the owner's own editor session and this
    # supervisor, which is exactly how a person learns to stop reading
    # the screen.
    territory = _territory(sessions)
    for pid, info in table.items():
        if info["state"] == "Z" or pid == os.getpid():
            continue
        cmdline = info["cmdline"]
        if not _is_vendor_cmdline(cmdline, config.vendor_markers):
            continue
        if any(pid in members for members in trees.values()):
            continue
        if not _inside(procs.working_directory(pid), territory):
            continue
        note_open("intruder", str(pid),
                  f"unclaimed vendor process {pid}: {cmdline[:200]}",
                  asserted)

    # Unregistered writers: ledger lines whose `by` the roster never minted.
    ledger_files: list[tuple[str, Path]] = [
        (name, paths.state_dir() / name) for name in SCAN_LEDGERS]
    try:
        fronts = sorted(Path(paths.fronts_dir()).iterdir())
    except OSError:
        fronts = []
    for front in fronts:
        for name in JOB_LEDGER_NAMES:
            ledger_files.append((f"{front.name}/{name}",
                                 front / name))
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

    monitors_view = _monitors_tick(moment, now_iso, note_open, asserted)

    # The clock this task builds: idle turn-contract sessions that heard
    # nothing since their last wake get a heartbeat. It only appends to
    # per-session event ledgers, so it cannot move a roster or job state.
    from . import wake as _wake

    _wake.heartbeat_tick(moment, config)

    # The clock's hands: every queued wake no turn is carrying gets a
    # detached `foreman turn` carrier. It only starts processes, so it
    # cannot move a roster or job state; the resolve pass below is
    # untouched by it.
    _carry_wakes(sessions, cstate)

    # Decision 32: the collector owns the queue tick. After observation
    # and the anomaly passes, start the oldest startable queued job per
    # front and rewrite why the rest wait.
    _queue_tick(cstate, now_iso)

    # Resolve what this tick no longer asserts. Unregistered-writer lines
    # clear only when the roster learns the id: the ledger line itself is
    # append-only, so absence from this tick's scan proves nothing. A dead
    # supervisor that is not replaced keeps its anomaly open until a live
    # session replaces it: resolving it on the next tick would hide the one
    # thing the owner must see.
    for (kind, subject), record in open_now.items():
        if (kind, subject) in asserted:
            continue
        if kind == "unregistered writer":
            if subject in sessions:
                store.append_ledger(paths.anomalies_path(),
                                    dict(record, resolved_at=now_iso))
            continue
        if kind == "supervisor dead":
            target = sessions.get(subject)
            if isinstance(target, dict) and \
                    (target.get("role") or "") == "supervisor":
                tpid = target.get("pid")
                tpid = tpid if isinstance(tpid, int) and tpid > 0 else None
                still_dead = tpid is None or tpid not in trees.get(
                    subject, set()) or not procs.same_process(
                        tpid, target.get("pid_starttime"), table)
                if still_dead and not _live_supervisor_for(
                        target.get("front"), sessions, table, trees):
                    continue
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
        "monitors": monitors_view,
    }
    store.write_snapshot(paths.observed_path(), observed_payload)
    if opened:
        # Hooks observe after the writes are durable, never inside them:
        # a failing hook prints and the tick's records stand either way.
        from . import hooks as _hooks

        for kind, subject, detail in opened:
            _hooks.fire("on-alert", {"kind": kind, "subject": subject,
                                     "detail": detail})
    return observed_payload


def tick(now: datetime | None = None,
         config: CollectorConfig | None = None) -> dict:
    """Run one observation tick. Returns the observed.json payload.

    The whole tick holds an exclusive lock so overlapping ticks cannot
    duplicate an anomaly line or relaunch decision.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    now_iso = moment.isoformat()
    config = config or load_config()
    with _exclusive_tick():
        observed = _tick_inner(moment, now_iso, config)
    # The panel's summary is derived from what this tick just wrote, so it
    # is refolded here and nowhere inside the tick: once per tick, after
    # the writes are durable, outside the lock.
    from . import panel_feed as _panel_feed

    _panel_feed.rewrite_quietly()
    return observed


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
    for jid, (front, record) in folded.items():
        entry: dict = {"state": record.get("state"),
                       "front": front}
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


def _front_monitor_decls() -> dict[str, tuple[dict, list[dict]]]:
    """Every front with a record: (folded record, its monitor list)."""
    try:
        names = sorted(entry.name for entry in Path(paths.fronts_dir()).iterdir()
                       if entry.is_dir())
    except OSError:
        return {}
    out: dict[str, tuple[dict, list[dict]]] = {}
    for name in names:
        try:
            records = store.read_ledger(paths.front_record_path(name))
        except OSError:
            continue
        folded = store.fold_by_id(records)
        if not folded:
            continue
        record = folded[-1]
        declared = record.get("monitors")
        declared = declared if isinstance(declared, list) else []
        out[name] = (record, [entry for entry in declared
                              if isinstance(entry, dict)])
    return out


def _monitors_tick(moment: datetime, now_iso: str,
                   note_open, asserted: set) -> dict:
    """Snapshot the latest measurement per monitor; flag stale and alert.

    Stale fires when the newest measurement is older than twice the
    monitor's ``every`` cadence, and only where ``every`` parses as a
    duration: an event cadence like ``landing`` or ``job`` carries no
    clock to be stale against. Alert fires when the monitor's alert
    expression holds of the latest value (the value/of ratio where a
    denominator is known, else the raw value). Both are anomalies of the
    same kind the collector already raises, under Problems. A monitor
    with no measurement yet is neither stale nor alerting.
    """
    from . import monitors as _monitors

    view: dict[str, dict] = {}
    for front, (_record, declared) in _front_monitor_decls().items():
        try:
            ledger = store.read_ledger(paths.front_measurements_path(front))
        except OSError:
            ledger = []
        per: dict[str, dict] = {}
        for decl in declared:
            measure = str(decl.get("measure") or "").strip()
            if not measure:
                continue
            matched = _monitors.matching_measurements(ledger, decl)
            if not matched:
                continue
            latest = matched[-1]
            previous = matched[-2] if len(matched) > 1 else None
            value = latest.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            stamped = latest.get("at")
            at_moment = _parse_time(stamped)
            age_s = (moment - at_moment).total_seconds() \
                if at_moment is not None else None
            question = str(decl.get("question") or measure)
            subject = f"{front}:{measure}"
            cadence = parse_duration(decl.get("every"))
            stale = bool(cadence is not None and age_s is not None
                         and age_s > 2 * cadence)
            if stale:
                assert isinstance(cadence, (int, float))
                note_open(
                    "monitor stale", subject,
                    f"monitor '{question}' on front '{front}' stale: "
                    f"last measurement "
                    f"{f'{age_s:.0f}s' if age_s is not None else 'unknown'} "
                    f"ago (every {decl.get('every')})",
                    asserted)
            effective = _monitors.effective_value(latest, decl)
            alert_text = decl.get("alert")
            alerting = bool(
                effective is not None and isinstance(alert_text, str)
                and alert_text.strip()
                and _monitors.evaluate_alert(effective, alert_text))
            if alerting:
                shown = latest.get("of")
                if isinstance(shown, (int, float)) and not isinstance(
                        shown, bool):
                    reading = (f"{_monitors.format_number(value)}/"
                               f"{_monitors.format_number(shown)}")
                else:
                    reading = _monitors.format_number(value)
                note_open(
                    "monitor alert", subject,
                    f"monitor '{question}' on front '{front}' alert: "
                    f"{reading} {str(alert_text).strip()}",
                    asserted)
            entry: dict = {
                "question": question,
                "measure": measure,
                "value": float(value),
                "at": stamped,
            }
            unit = decl.get("unit")
            if isinstance(unit, str) and unit.strip():
                entry["unit"] = unit.strip()
            for key in ("of",):
                val = latest.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    entry[key] = float(val)
            every = decl.get("every")
            if isinstance(every, str) and every.strip():
                entry["every"] = every.strip()
            if isinstance(alert_text, str) and alert_text.strip():
                entry["alert"] = alert_text.strip()
            entry["stale"] = stale
            entry["alerting"] = alerting
            prev_value = previous.get("value") if isinstance(
                previous, dict) else None
            if isinstance(prev_value, bool) or not isinstance(
                    prev_value, (int, float)):
                prev_value = None
            entry["trend"] = _monitors.trend_of(
                float(prev_value) if prev_value is not None else None,
                float(value))
            per[measure] = entry
        if per:
            view[front] = per
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


def _service_active() -> bool:
    """True when the collector runs as an active user service."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", COLLECTOR_UNIT],
            capture_output=True, timeout=10)
    except (OSError, ValueError):
        return False
    return proc.returncode == 0


def _service_restart() -> int:
    """Restart the collector user service; never raises on a missing setup."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "restart", COLLECTOR_UNIT],
            capture_output=True, text=True, timeout=60)
    except (OSError, ValueError) as exc:
        print(f"foreman collector: cannot restart {COLLECTOR_UNIT}: {exc}")
        return 1
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        print(f"foreman collector: restart failed"
              f"{f': {detail}' if detail else ''}; run "
              f"`systemctl --user restart {COLLECTOR_UNIT}` by hand")
        return proc.returncode
    return 0


def restart_collector(quiet: bool = False) -> int:
    """Restart the collector where it runs as a user service.

    Where it does not, say plainly what to run instead: the person reading
    the stale screen is whoever must run it. When quiet, a machine with no
    service to restart is silence rather than guidance: the verbs that
    changed a ceiling call this after every change, and their own line is
    the whole of what they owe the owner.
    """
    if _service_active():
        if _service_restart() == 0:
            print(f"collector service {COLLECTOR_UNIT} restarting")
            return 0
        return 1
    if quiet:
        return 0
    print(f"collector is not running as a user service; start it with "
          f"`systemctl --user start {COLLECTOR_UNIT}`, or run "
          f"`foreman collector run` in the foreground")
    return 0


def reload_after_config_change() -> None:
    """Push a collector reload after a verb changed what it observes.

    `foreman cap` and `front allocate` call this once their own write is
    durable, so the `collector stale` line clears with no hand restart.

    Only on the default world. A process running against an isolated
    ``FOREMAN_STATE`` or ``FOREMAN_CONFIG`` — every test, and every proof
    run — has no service of its own, and the one service on the machine
    observes somebody else's state: restarting it there is a verb in a
    sandbox reaching out and bouncing the owner's live collector. Found
    by a test that flaked because a ceiling change in it restarted the
    real one.
    """
    if any(os.environ.get(name) for name in (paths.STATE_ENV,
                                             paths.CONFIG_ENV)):
        return
    restart_collector(quiet=True)


def collector_main(action: str) -> int:
    if action == "unit":
        print(render_unit(), end="")
        return 0
    if action == "once":
        tick()
        return 0
    if action == "restart":
        return restart_collector()
    record_startup_version()
    while True:
        # The configuration is re-read on every tick, never held from
        # startup: a cap the owner changed mid-run governs the next tick's
        # totals with no restart. No file watcher — reading a small TOML
        # file once every two seconds is cheaper than being wrong.
        config = load_config()
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
    parser.add_argument("action", choices=("run", "once", "unit", "restart"),
                        help="run the daemon, run one tick, print the "
                             "systemd user unit, or restart the daemon")


@subcommand("collector", help="Observe the swarm, flag anomalies.")
def _collector_entry(args: argparse.Namespace) -> int:
    from . import caller as _caller

    me, violations = _caller.resolve("collector")
    if args.action != "restart":
        _caller.check_role(me, "collector", violations=violations)
    # Restart is the owner's verb, and any caller with a role may call it
    # too: the person who notices the stale screen is whoever reads it.
    if violations:
        from .caller import Refusal as _Refusal
        return _Refusal(violations).report()
    try:
        return collector_main(args.action)
    except KeyboardInterrupt:
        return 0


_collector_entry.add_arguments = add_collector_arguments  # type: ignore[attr-defined]
