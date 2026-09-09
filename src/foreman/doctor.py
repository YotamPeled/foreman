"""`foreman doctor`: recorded against observed, every divergence with a fix.

Six cross-checks, each naming the command that repairs it:

1. roster sessions (worker roles and supervisors) whose process is gone
   or whose pid now belongs to another process — a pid alone is not an
   identity, so the recorded start time must still match;
2. git worktrees under the state directory no job owns, and live jobs
   whose worktree is gone (a job on a done front is history, not a
   live failure; the printed fix is ``job fail --closed``);
3. open slot grants whose session is not running;
4. ``foreman.toml`` pool tables naming pools no adapter registers;
5. the collector running code older than this checkout;
6. front records from before the merge mode existed (``foreman migrate``
   backfills them);
7. the installed ``foreman`` command running from a checkout with a
   branch out instead of the main-only checkout it must run from.

Exit 0 with ``doctor: clean`` when everything agrees, 1 with the count
when anything does not, so it reads as a shell condition. Doctor only
reads: every repair is a printed command, never taken.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from . import caller, capacity, cli, fronts, paths, procs, store
from .caller import FOREMAN, MERGE_DESK, SUPERVISOR, Refusal
from .cli import subcommand
from .collector import _checkout_root, _git_head, _package_dir, _source_mtime
from .entities import SESSION_ROLES

#: Roster states a session is still supposed to be alive in. Anything
#: else (exited, killed, failed) is the collector's closed book, not a
#: divergence.
LIVE_SESSION_STATES = ("running", "starting", "stalled")

#: Job states whose worktree should still be where the record says.
#: Terminal jobs (verified, failed, killed, history) keep no such
#: promise: only a failed spawn cleans its worktree up, and a landed
#: branch outlives the directory it was built in. A job that returned
#: with work still needs its worktree: the supervisor has yet to verify
#: what it holds. A job on a done front is history, not this promise.
LIVE_JOB_STATES = ("planned", "queued", "running", "returned",
                   "returned-with-work")

#: Session roles doctor checks liveness for. The foreman's own session
#: is the orchestrator's interactive process (out of band: no verb mints
#: it, so no verb repairs it), and a dead merge-desk entry likewise has
#: no clearing verb — flagging either would print a divergence with no
#: fix, which is exactly what the fix line below each finding forbids.
CHECKED_ROLES = tuple(SESSION_ROLES)


def _problems() -> list[tuple[str, str]]:
    """Every (divergence, fix) pair, in check order."""
    found: list[tuple[str, str]] = []
    found.extend(_check_sessions())
    found.extend(_check_worktrees())
    found.extend(_check_slots())
    found.extend(_check_config())
    found.extend(_check_collector_staleness())
    found.extend(_check_front_records())
    found.extend(_check_cli_branch())
    found.extend(_check_default_workspace())
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
        elif role == MERGE_DESK:
            # A dead desk is worse than a dead worker: it may hold a merge
            # it took, and nothing else may take it while the record says
            # it is being merged. Nobody was told, because the roles this
            # checked were the job roles and the supervisor only.
            fix = "foreman launch merge-desk"
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
        if os.path.isdir(worktree):
            continue
        front_record = fronts.read_front_record(front)
        if (front_record or {}).get("state") == "done":
            # The work landed and the front closed; failing the job
            # would rewrite that as a failure. History, with a flag
            # that records it as history, is what is true.
            out.append((
                f"job {jid} on done front '{front}' is history "
                f"(worktree {worktree} is gone)",
                f"foreman job fail {jid} --closed"))
        else:
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


def _check_default_workspace() -> list[tuple[str, str]]:
    """A configuration with no workspace for a windowed launch to open on.

    Required configuration since a windowed launch stopped waiting thirty
    seconds for a window that could never appear: `--workspace` names one
    per launch, and this is what every launch that does not falls back
    to. The collector's automatic relaunch of a dead supervisor has no way
    to name one, so without this value flow 4 stops with a refusal on the
    daemon's stderr and the front sits without a supervisor.
    """
    from .launch import configured_default_workspace

    if configured_default_workspace() is not None:
        return []
    path = paths.config_file()
    return [(f"config {path} sets no [launch] default_workspace; a windowed "
             f"launch that names none is refused, and the collector cannot "
             f"relaunch a dead supervisor",
             f"add a `[launch]` table to {path} with "
             f"`default_workspace = <n>`")]


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


#: The branch the installed command runs from. A checkout with any other
#: branch out is a half-built runtime for the whole swarm, so the install
#: is a main-only checkout the post-merge hook fast-forwards (see README
#: "The installed command").
MAIN_BRANCH = "main"


def _invoked_as_installed_cli() -> bool:
    """True when this process is the installed `foreman` command itself.

    Anything else — `python -m foreman`, a test calling the entry point in
    process, a script importing it — runs whatever checkout it runs from
    by choice, and has no branch to be on the wrong one of.
    """
    return Path(sys.argv[0]).name in ("foreman", "foreman.exe")


def _package_checkout() -> Path | None:
    """The checkout this package runs from, or None for an installed copy.

    A checkout is the first directory above this file holding a `.git`
    entry; an installed wheel or a copied tree answers None, never an
    error.
    """
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        try:
            if (parent / ".git").exists():
                return parent
        except OSError:
            continue
    return None


def _checkout_branch(root: Path) -> str | None:
    """The branch checked out at ``root``, or None where there is none.

    Detached, unborn, missing git and not-a-checkout all answer None: no
    branch out is nothing to report, and doctor only reads.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _installed_cli_branch() -> tuple[str, str] | None:
    """(checkout, branch) for the installed CLI, or None where unknowable."""
    if not _invoked_as_installed_cli():
        return None
    root = _package_checkout()
    if root is None:
        return None
    branch = _checkout_branch(root)
    if branch is None:
        return None
    return (str(root), branch)


def _check_cli_branch() -> list[tuple[str, str]]:
    """The installed CLI running off a branch instead of the main one."""
    found = _installed_cli_branch()
    if found is None:
        return []
    root, branch = found
    if branch == MAIN_BRANCH:
        return []
    return [(f"foreman runs from branch '{branch}' in {root} "
             f"(the installed command must run from a main-only checkout)",
             "reinstall foreman from the main-only checkout, then run: "
             "foreman collector restart")]


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
