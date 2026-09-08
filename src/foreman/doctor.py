"""`foreman doctor`: recorded against observed, every divergence with a fix.

Six cross-checks, each naming the command that repairs it:

1. roster sessions (worker roles and supervisors) whose process is gone
   or whose pid now belongs to another process — a pid alone is not an
   identity, so the recorded start time must still match;
2. git worktrees under the state directory no job owns, and live jobs
   whose worktree is gone;
3. open slot grants whose session is not running;
4. ``foreman.toml`` pool tables naming pools no adapter registers;
5. the collector running code older than this checkout;
6. front records from before the merge mode existed (``foreman migrate``
   backfills them).

Exit 0 with ``doctor: clean`` when everything agrees, 1 with the count
when anything does not, so it reads as a shell condition. Doctor only
reads: every repair is a printed command, never taken.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from pathlib import Path

from . import caller, capacity, cli, fronts, paths, procs, store
from .caller import FOREMAN, SUPERVISOR, Refusal
from .cli import subcommand
from .collector import _checkout_root, _git_head, _package_dir, _source_mtime
from .entities import JOB_ROLES

#: Roster states a session is still supposed to be alive in. Anything
#: else (exited, killed, failed) is the collector's closed book, not a
#: divergence.
LIVE_SESSION_STATES = ("running", "starting", "stalled")

#: Job states whose worktree should still be where the record says.
#: Terminal jobs (verified, failed, killed) keep no such promise: only
#: a failed spawn cleans its worktree up, and a landed branch outlives
#: the directory it was built in.
LIVE_JOB_STATES = ("planned", "queued", "running", "returned")

#: Session roles doctor checks liveness for. The foreman's own session
#: is the orchestrator's interactive process (out of band: no verb mints
#: it, so no verb repairs it), and a dead merge-desk entry likewise has
#: no clearing verb — flagging either would print a divergence with no
#: fix, which is exactly what the fix line below each finding forbids.
CHECKED_ROLES = tuple(JOB_ROLES) + (SUPERVISOR,)


def _problems() -> list[tuple[str, str]]:
    """Every (divergence, fix) pair, in check order."""
    found: list[tuple[str, str]] = []
    found.extend(_check_sessions())
    found.extend(_check_worktrees())
    found.extend(_check_slots())
    found.extend(_check_config())
    found.extend(_check_collector_staleness())
    found.extend(_check_front_records())
    return found


def _roster_sessions() -> dict:
    roster = store.read_snapshot(paths.roster_path(), default=None)
    if not isinstance(roster, dict):
        return {}
    sessions = roster.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def _check_sessions() -> list[tuple[str, str]]:
    """Live roster sessions whose process is gone or is somebody else's."""
    out: list[tuple[str, str]] = []
    table = procs.snapshot()
    for sid, record in _roster_sessions().items():
        if not isinstance(record, dict):
            continue
        if record.get("state") not in LIVE_SESSION_STATES:
            continue
        if record.get("role") not in CHECKED_ROLES:
            continue
        pid = record.get("pid")
        pid = pid if isinstance(pid, int) and pid > 0 else None
        if pid is None:
            # No process recorded, nothing observed to compare it with:
            # the collector likewise reads pid-less sessions as unknown,
            # never as dead.
            continue
        stored = record.get("pid_starttime")
        current = (table.get(pid) or {}).get("starttime") \
            if table else procs.proc_starttime(pid)
        alive = procs.pid_alive(pid)
        if stored is None:
            # A legacy record without identity: trusted, like the
            # collector trusts it, while its process answers at all.
            if alive:
                continue
            detail = f"process {pid} is gone"
        elif current is not None and current == stored and alive:
            continue
        elif alive and current is not None:
            detail = (f"pid {pid} is now a different process "
                      f"(recorded start time no longer matches)")
        elif alive:
            # The process answers but its start time cannot be read:
            # somebody else's, unobservable — not evidence of death.
            continue
        else:
            detail = f"process {pid} is gone"
        role, job = record.get("role"), record.get("job")
        if role == SUPERVISOR:
            fix = f"foreman relaunch {sid}"
        else:
            # The collector closes dead workers itself on its next tick
            # (job killed or returned, slots released, session exited):
            # running one tick by hand is the repair.
            fix = "foreman collector once"
            if isinstance(job, str) and job:
                fix = (f"foreman job fail {job} --finding "
                       f"'session {sid} is gone'")
        out.append((f"stale session {sid} (role {role}): {detail}", fix))
    return out


def _folded_jobs() -> dict[str, tuple[str, dict]]:
    """Every job ledger line folded last-wins: id -> (front, record)."""
    folded: dict[str, tuple[str, dict]] = {}
    try:
        names = sorted(entry.name for entry in paths.fronts_dir().iterdir()
                       if entry.is_dir())
    except OSError:
        return folded
    for name in names:
        try:
            records = store.read_ledger(paths.front_jobs_path(name))
        except OSError:
            continue
        for record in store.fold_by_id(records):
            jid = record.get("id")
            if isinstance(jid, str) and jid:
                folded[jid] = (name, record)
    return folded


def _check_worktrees() -> list[tuple[str, str]]:
    """Worktree dirs no job owns, and live jobs whose worktree is gone."""
    out: list[tuple[str, str]] = []
    owned: set[str] = set()
    live: list[tuple[str, str, str, str]] = []
    for jid, (front, record) in _folded_jobs().items():
        worktree = record.get("worktree")
        if not (isinstance(worktree, str) and worktree):
            continue
        owned.add(os.path.abspath(worktree))
        state = record.get("state")
        if state in LIVE_JOB_STATES:
            live.append((jid, front, worktree, state))
    root = paths.state_dir() / "worktrees"
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        entries = []
    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if not is_dir:
            continue
        if os.path.abspath(str(entry)) not in owned:
            out.append((
                f"orphan worktree {entry} (no job owns it)",
                f"git worktree remove --force {entry}"))
    for jid, front, worktree, state in sorted(live):
        if not os.path.isdir(worktree):
            out.append((
                f"job {jid} on front '{front}' is '{state}' "
                f"but its worktree {worktree} is gone",
                f"foreman job fail {jid} --finding "
                f"'worktree {worktree} is gone'"))
    return out


def _check_slots() -> list[tuple[str, str]]:
    """Open slot grants held by sessions that are not running."""
    out: list[tuple[str, str]] = []
    sessions = _roster_sessions()
    for grant in capacity.open_grants():
        if not isinstance(grant, dict):
            continue
        sid = grant.get("session")
        if not (isinstance(sid, str) and sid):
            continue
        record = sessions.get(sid)
        if isinstance(record, dict) and \
                record.get("state") in LIVE_SESSION_STATES:
            continue
        state = (record or {}).get("state") if isinstance(record, dict) \
            else "(no roster entry)"
        out.append((
            f"open slot grant {grant.get('id') or '(no id)'} "
            f"(pool '{grant.get('pool')}', front '{grant.get('front')}') "
            f"held by session {sid} ({state})",
            "foreman collector once"))
    return out


def _config_pools() -> tuple[dict, Path, str | None]:
    """The user's pool tables, the file they came from, and a parse error."""
    path = paths.config_file()
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return {}, path, None
    except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
        return {}, path, str(exc)
    if not isinstance(raw, dict):
        return {}, path, "not a TOML table"
    pools: dict = {}
    # `[pools.<name>]` is the older spelling the collector still reads,
    # so a table under either spelling configures (or misconfigures).
    for table_name in ("pool", "pools"):
        table = raw.get(table_name)
        if isinstance(table, dict):
            for name, entry in table.items():
                if isinstance(name, str):
                    pools[name] = entry
    return pools, path, None


def _check_config() -> list[tuple[str, str]]:
    """Pool tables naming pools no adapter registers."""
    from .pools import names as pool_names

    out: list[tuple[str, str]] = []
    pools, path, error = _config_pools()
    if error is not None:
        return [(f"config {path} does not parse ({error}); "
                 f"the packaged defaults answer until it does",
                 f"edit {path} until it parses")]
    registered = set(pool_names())
    for name in sorted(pools):
        if name not in registered:
            out.append((
                f"config {path} caps pool '{name}' that no adapter "
                f"registers, so that cap governs nothing",
                f"remove the [pool.'{name}'] table from {path}, "
                f"or make it real with: foreman pool add {name}"))
    return out


def _check_collector_staleness() -> list[tuple[str, str]]:
    """The collector running code older than this checkout."""
    try:
        state = store.read_snapshot(paths.collector_path(), default=None)
    except (OSError, ValueError):
        return []
    if not isinstance(state, dict):
        return []
    if "code_head" not in state and "code_mtime" not in state:
        # Never recorded (collector never ran): nothing is running stale.
        return []
    recorded_head = state.get("code_head")
    recorded_mtime = state.get("code_mtime")
    try:
        current_head = _git_head(_checkout_root())
        current_mtime = _source_mtime(_package_dir())
    except (OSError, ValueError):
        return []
    head_stale = (
        isinstance(recorded_head, str) and bool(recorded_head)
        and isinstance(current_head, str) and bool(current_head)
        and current_head != recorded_head)
    mtime_stale = (
        isinstance(recorded_mtime, (int, float))
        and isinstance(current_mtime, (int, float))
        and current_mtime > recorded_mtime)
    if not (head_stale or mtime_stale):
        return []
    return [("collector is running code older than this checkout",
             "foreman collector restart")]


def _check_front_records() -> list[tuple[str, str]]:
    """Front records from before the merge mode existed."""
    out: list[tuple[str, str]] = []
    try:
        names = sorted(entry.name for entry in paths.fronts_dir().iterdir()
                       if entry.is_dir())
    except OSError:
        return out
    for name in names:
        record = fronts.read_front_record(name)
        if record is None or "merge" in record:
            continue
        out.append((
            f"front '{name}' has no merge mode (predates the merge desk)",
            "foreman migrate"))
    return out


def doctor_main() -> int:
    verb = "doctor"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, FOREMAN, SUPERVISOR, violations=violations)
    if violations:
        return Refusal(violations).report()
    found = _problems()
    for divergence, fix in found:
        print(f"doctor: {divergence}")
        print(f"  fix: {fix}")
    if not found:
        print("doctor: clean")
        return 0
    print(f"doctor: {len(found)} problem(s)")
    return 1


@subcommand("doctor", help="Cross-check recorded against observed.")
def _doctor_entry(args: argparse.Namespace) -> int:
    return doctor_main()
