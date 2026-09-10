"""Slot grants: what a pool holds, what a front may hold, who is waiting.

Held is derived, never stored. A slot is held while its grant line carries
no ``released_at``, so the number of held slots is a fold over
``slots.jsonl`` and nothing else — there is no counter to increment, none
to decrement, and none to drift out of step with the swarm when a session
dies in a way nobody wrote code for. The ledger is append-only like every
other one: a grant is released by appending a revised copy of its own line
carrying ``released_at`` and ``released_because``, and every reader folds
last-wins.

The two limits are different things. A **pool cap** is the whole system's
limit on one model family. A front's **allocation** is a ceiling per role
— the most that front may hold at once — and is the ceiling only while
nothing is reserved. An open reservation on ``reservations.jsonl`` holds
those slots against the cap until it is released: ``ceiling`` reads the
reserved count, and the pool check treats every other front's open
reservation as already held.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import config, entities, fronts, ids, paths, procs, store

#: Work not yet running: a job in one of these states is waiting for a slot.
QUEUE_STATES = ("planned", "queued")

#: The roster role of a front's own session. It draws on a pool like any
#: worker, and is counted against that pool's ``supervisor_cap``.
SUPERVISOR_ROLE = "supervisor"


def pool_for_role(role: str) -> str:
    """The registered pool a role runs on.

    The mapping is declared in one place — ``roles`` beside the pool in the
    configuration (:mod:`foreman.config`) — because a role and its pool are
    only the same word by accident: an ``opus`` worker runs on ``claude``
    and an ``astra`` reviewer on ``codex``. Returning the role unchanged is
    what counted queued work against pools that do not exist. A role no
    pool claims names its own pool, which is what a launch that supplies
    both already does.
    """
    return config.load().pool_for_role(role) or role


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


def _grant_key(record: dict):
    """What identifies a grant across its revisions.

    New grants carry a minted id and fold on it. Grants written before ids
    were minted have none, so they fold on the four fields that were their
    identity: the pool, the session, the job and the moment granted.
    """
    rid = record.get("id")
    if isinstance(rid, str) and rid:
        return rid
    return ("legacy", record.get("pool"), record.get("session"),
            record.get("job"), record.get("granted_at"))


def folded_grants() -> list[dict]:
    """Every grant, folded last-wins, in first-appearance order."""
    try:
        records = store.read_ledger(paths.slots_path())
    except OSError:
        return []
    order: list = []
    latest: dict = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        key = _grant_key(record)
        if key not in latest:
            order.append(key)
        latest[key] = record
    return [latest[key] for key in order]


def open_grants() -> list[dict]:
    return [grant for grant in folded_grants()
            if grant.get("released_at") is None]


def held_by_pool() -> dict[str, int]:
    held: dict[str, int] = {}
    for grant in open_grants():
        pool = grant.get("pool")
        if isinstance(pool, str) and pool:
            held[pool] = held.get(pool, 0) + 1
    return held


def held_by_front_role() -> dict[tuple[str, str], int]:
    held: dict[tuple[str, str], int] = {}
    for grant in open_grants():
        front, role = grant.get("front"), grant.get("role")
        if isinstance(front, str) and front and \
                isinstance(role, str) and role:
            held[(front, role)] = held.get((front, role), 0) + 1
    return held


def _grant_record(*, pool: str, front: str | None, role: str,
                  job: str | None, session: str | None) -> dict:
    return entities.SlotGrant(
        id=ids.mint("slot"), pool=pool, front=front or "", role=role,
        job=job, session=session, granted_at=store.utcnow_iso(),
        released_at=None, released_because="",
    ).to_dict()


def grant(*, pool: str, front: str | None, role: str, job: str | None,
          session: str | None) -> dict:
    """Take one slot for a job that is starting. Appended, never counted."""
    return store.append_ledger(
        paths.slots_path(),
        _grant_record(pool=pool, front=front, role=role, job=job,
                      session=session),
        session_id=session)


def _append_grant_holding_the_lock(record: dict,
                                   session: str | None) -> dict:
    """Append one grant line while the store's write lock is already held.

    This is :func:`store.append_ledger`'s write with the lock taken out of
    it, and it exists for exactly one caller, :func:`admit`. ``flock`` is
    held per open file description, not per process, so a second
    ``append_ledger`` inside the lock would block on the lock this process
    is already holding and never wake up. The alternative — a second lock
    file of capacity's own — would make two locks where the state directory
    has one, so the write is inlined here instead.
    """
    entry = dict(record)
    entry.setdefault("at", store.utcnow_iso())
    if session is not None:
        entry.setdefault("by", session)
    ledger = Path(paths.slots_path())
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def release_for_session(session_id: str, when: str, because: str) -> int:
    """Give back every slot this session still holds. Returns how many.

    The release is an appended revised copy of the grant's own line, so the
    ledger only ever grows and a grant already released is not released
    twice.
    """
    released = 0
    for record in folded_grants():
        if record.get("session") != session_id:
            continue
        if record.get("released_at") is not None:
            continue
        store.append_ledger(
            paths.slots_path(),
            dict(record, released_at=when, released_because=because))
        released += 1
    return released


# --------------------------------------------------------------------------
# The two limits
# --------------------------------------------------------------------------


def _snapshot() -> dict[int, dict]:
    """The live process table. A test substitutes this, never ``procs.snapshot``
    itself: the collector still has to see the children it started."""
    return procs.snapshot()


def running_by_pool(table: dict[int, dict] | None = None) -> dict[str, int]:
    """Vendor processes on the machine, by pool.

    ``table`` is a process table of the shape :func:`procs.snapshot`
    returns (pid -> ``{cmdline, ...}``). ``None`` takes a fresh snapshot.
    A process counts for a pool when
    ``procs.executable_of(cmdline)`` equals that pool's adapter
    ``binary`` — argv[0]'s basename, never a substring of the rest of
    the line. A pool that names no binary counts nothing, never
    everything.
    """
    from . import pools as poolmod

    if table is None:
        table = _snapshot()
    counts: dict[str, int] = {}
    for name in poolmod.names():
        try:
            adapter = poolmod.get(name)
        except ValueError:
            continue
        binary = getattr(adapter, "binary", "") or ""
        if not isinstance(binary, str) or not binary:
            counts[name] = 0
            continue
        n = 0
        for info in table.values():
            if not isinstance(info, dict):
                continue
            if info.get("state") == "Z":
                continue
            cmdline = info.get("cmdline", "") or ""
            if procs.executable_of(cmdline) == binary:
                n += 1
        counts[name] = n
    return counts


def _binary_is_foreman_worker(pool: str) -> bool:
    """True when this pool's vendor binary is only ever a Foreman worker.

    Read off the adapter. A pool that does not declare it, or that
    nothing registers, defaults to True.
    """
    from . import pools as poolmod

    try:
        adapter = poolmod.get(pool)
    except ValueError:
        return True
    return bool(getattr(adapter, "binary_is_foreman_worker", True))


def ceiling(front: str | None, role: str) -> int | None:
    """The most sessions ``front`` may hold of ``role``, or None for none.

    None means "no ceiling at all", and only a front with no record on the
    ledger gets it: nothing said what that front may hold, so only the pool
    cap speaks. A front that does have a record is held to it, and a role
    its allocation does not name is allocated nothing.

    An open reservation replaces the allocation: the reserved count for
    this role is the ceiling, including zero for a role the front did
    not reserve. The allocation is the ceiling only when nothing is
    reserved.
    """
    if not front:
        return None
    record = fronts.read_front_record(front)
    if record is None:
        return None
    if fronts.front_has_open_reservation(front):
        value = fronts.reserved_by_front_role().get((front, role), 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value
    allocation = record.get("allocation")
    value = allocation.get(role) if isinstance(allocation, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _who(role: str, front: str | None) -> str:
    """Who a refusal is about: the role, and the front when there is one.

    Every capacity refusal opens the same way, so a supervisor reading two
    of them side by side is reading the same sentence with different
    numbers. A refusal that named only the pool left the reader to work out
    which of its launches had been stopped.
    """
    return (f"role {role!r} on front {front!r}" if front
            else f"role {role!r} on no front")


def _as_utc(now: datetime | None) -> datetime:
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _parse_iso(text: object) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def format_out_until(moment: datetime) -> str:
    """The Capacity spelling of a reset: ``2026-09-14 00:00Z``."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def folded_pools() -> list[dict]:
    """Every pool-state record, folded last-wins by pool name."""
    try:
        records = store.read_ledger(paths.pools_path())
    except OSError:
        return []
    return store.fold_by_id(records)


def pool_out(pool: str, now: datetime | None = None) -> dict | None:
    """The folded record if ``pool`` is out at ``now``, else None.

    A record whose ``out_until`` has passed is not out: the fold is read
    against now, and nothing rewrites the ledger to clear it. An
    unparseable reset is not out either — a pool is never put out on a
    guess.
    """
    moment = _as_utc(now)
    for record in folded_pools():
        name = record.get("id")
        if name != pool:
            continue
        until = _parse_iso(record.get("out_until"))
        if until is None or moment >= until:
            return None
        return record
    return None


def out_pools(now: datetime | None = None) -> dict[str, dict]:
    """Pool name -> folded record, for every pool that is out at ``now``."""
    moment = _as_utc(now)
    out: dict[str, dict] = {}
    for record in folded_pools():
        name = record.get("id")
        if not isinstance(name, str) or not name:
            continue
        until = _parse_iso(record.get("out_until"))
        if until is None or moment >= until:
            continue
        out[name] = record
    return out


def allocation_out(role: str, now: datetime | None = None) -> str | None:
    """``out until <reset> (because)`` if ``role``'s pool is out, else None.

    Both places a front's allocation is rendered read this, so a pool
    that is out cannot print held on one screen and out on the other.
    """
    record = pool_out(pool_for_role(role), now)
    if record is None:
        return None
    until = _parse_iso(record.get("out_until"))
    because = record.get("because") or "quota"
    when = format_out_until(until) if until is not None else ""
    return f"out until {when} ({because})"


def launch_problems(role: str, pool: str, front: str | None,
                    now: datetime | None = None,
                    table: dict[int, dict] | None = None) -> list[str]:
    """The capacity refusals for one launch, in the order they are checked.

    The front's ceiling first, then the pool's cap against held slots,
    then the same cap against vendor processes on the machine (only
    for a pool whose binary is only ever a Foreman worker), then
    whether the pool is out until a named reset. Held and the box are
    two numbers against one cap: the held refusal fires for every
    pool, the box refusal only for a pool that declares its binary
    Foreman's. Each names the role, the front, and the limit it hit.
    A launch wrong on more than one count is told every one at once,
    like every other refusal here.

    ``table`` is the process table the box count is read from; ``None``
    takes a fresh snapshot.
    """
    problems: list[str] = []
    limit = ceiling(front, role)
    if limit is not None:
        held = held_by_front_role().get((front or "", role), 0)
        if held >= limit:
            if front and fronts.front_has_open_reservation(front):
                problems.append(f"{_who(role, front)}: "
                                f"{held} held, reservation {limit}")
            else:
                problems.append(f"{_who(role, front)}: "
                                f"{held} held, ceiling {limit}")
    cap = config.load().cap(pool)
    if cap is not None:
        held = held_by_pool().get(pool, 0)
        reserved_others = fronts.reserved_by_pool(except_front=front)
        reserved = reserved_others.get(pool, 0)
        if held + reserved >= cap:
            if reserved:
                problems.append(
                    f"{_who(role, front)}: {held} held in pool "
                    f"{pool!r}, reserved {reserved}, cap {cap}")
            else:
                problems.append(f"{_who(role, front)}: {held} held in pool "
                                f"{pool!r}, cap {cap}")
        running = running_by_pool(table).get(pool, 0)
        if running >= cap and _binary_is_foreman_worker(pool):
            problems.append(f"{_who(role, front)}: {running} {pool} "
                            f"processes on the box, cap {cap}")
    record = pool_out(pool, now)
    if record is not None:
        until = _parse_iso(record.get("out_until"))
        because = record.get("because") or "quota"
        when = format_out_until(until) if until is not None else ""
        problems.append(f"{_who(role, front)}: pool {pool!r} is out "
                        f"until {when} ({because})")
    return problems


def admit(*, role: str, pool: str, front: str | None, job: str | None,
          session: str | None) -> list[str]:
    """Check the two limits and take the slot, as one operation.

    Returns the refusals, and takes nothing when there are any; returns an
    empty list and holds one slot when there are none.

    Checking and granting cannot be two operations. Between them the ledger
    says the slot is free while a launch that has already passed the check
    is on its way to taking it, so two launchers racing each other both read
    room for one and both take it — a cap of one holding two workers, and a
    screen saying the swarm is inside a budget it is outside of. Both halves
    are done here under the store's one write lock, so the losing launcher
    reads the winner's grant and is refused by the same sentence it would
    have read a second later.

    Holding the lock across a check and an append is a moment, not the
    front's reservation: an open line on ``reservations.jsonl`` is what
    holds the team, and ``launch_problems`` already counted it. What is
    held afterwards is a running worker's slot.
    """
    with store._write_lock():
        problems = launch_problems(role, pool, front)
        if problems:
            return problems
        _append_grant_holding_the_lock(
            _grant_record(pool=pool, front=front, role=role, job=job,
                          session=session), session)
    return []


def supervisor_problems(front: str | None, held: int) -> list[str]:
    """The refusal for a supervisor summoned past the supervisor cap.

    ``supervisor_cap`` is a limit of its own and not a share of ``cap``: a
    supervisor holds no job slot, so counting it against the pool's job cap
    would let one busy front's workers lock every other front out of a
    supervisor. It is counted across every front, because a per-front count
    is the "one front, one supervisor" rule and is checked separately.

    ``held`` is the number of live supervisors, which only the launcher can
    say: a roster record is a supervisor only while its process is still
    that process.
    """
    pool = pool_for_role(SUPERVISOR_ROLE)
    cap = config.load().supervisor_cap(pool)
    if cap is None or held < cap:
        return []
    return [f"{_who(SUPERVISOR_ROLE, front)}: {held} held across every "
            f"front, supervisor cap {cap} on pool {pool!r}"]


# --------------------------------------------------------------------------
# The Capacity block
# --------------------------------------------------------------------------


def waiting_by_pool() -> dict[str, int]:
    """Jobs on a front ledger that are planned or queued, by the pool their
    role runs on."""
    waiting: dict[str, int] = {}
    try:
        names = sorted(entry.name for entry in paths.fronts_dir().iterdir()
                       if entry.is_dir())
    except OSError:
        return waiting
    for name in names:
        try:
            jobs = store.fold_by_id(
                store.read_ledger(paths.front_jobs_path(name)))
        except OSError:
            continue
        for job in jobs:
            if job.get("state") not in QUEUE_STATES:
                continue
            role = job.get("role")
            if not (isinstance(role, str) and role):
                continue
            pool = pool_for_role(role)
            waiting[pool] = waiting.get(pool, 0) + 1
    return waiting


def front_allocations() -> list[tuple[str, str, int]]:
    """(front, role, ceiling) for every role a front record allocates.

    A front at state ``done`` allocates nothing: a ceiling on a front
    nobody is working is not capacity, so its lines stay off the one
    screen that has to stay readable.
    """
    rows: list[tuple[str, str, int]] = []
    try:
        names = sorted(entry.name for entry in paths.fronts_dir().iterdir()
                       if entry.is_dir())
    except OSError:
        return rows
    for name in names:
        record = fronts.read_front_record(name)
        if (record or {}).get("state") == "done":
            continue
        allocation = (record or {}).get("allocation")
        if not isinstance(allocation, dict):
            continue
        for role in sorted(allocation):
            value = allocation[role]
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            rows.append((name, role, value))
    return rows


def capacity_lines(observed: dict | None,
                   now: datetime | None = None,
                   table: dict[int, dict] | None = None) -> list[str]:
    """The Capacity block: held/cap per pool, held/ceiling per front role,
    and who is waiting on a pool that has nothing left to give.

    Held comes from the slot ledger, so the block is right whether or not
    the collector has ticked. Every pool's cap comes from the configuration
    read at the moment this runs, never from the collector's cached copy:
    the collector wrote its totals with the caps in force when it started,
    so reading them here would keep showing the old ceiling after `foreman
    cap` until the collector is restarted. Only the held counts ride along
    in observed.json.

    A pool that is out until a named reset prints that instead of
    held/cap, for as long as the reset is in the future. A front's
    allocation over such a pool prints the same out line in place of
    held/ceiling. The box count — vendor processes on the machine,
    whoever started them — prints on the pool's row when it differs
    from held; a pool whose two numbers agree keeps the row it printed
    before.

    Open reservations change the spelling: a pool with any prints
    ``held h / reserved r / cap c``, and a front that holds one prints
    ``reserved r, running h`` per role. Fronts with nothing reserved
    keep the old held/ceiling line.

    ``table`` is the process table the box count is read from; ``None``
    takes a fresh snapshot.
    """
    pool_view = (observed or {}).get("pools")
    pool_view = pool_view if isinstance(pool_view, dict) else {}
    held_pool = held_by_pool()
    allocations = front_allocations()
    held_front = held_by_front_role()
    reserved_pool = fronts.reserved_by_pool()
    reserved_front_role = fronts.reserved_by_front_role()
    holding = fronts.reserved_fronts()
    out_now = out_pools(now)
    running_pool = running_by_pool(table)

    names = set(held_pool)
    names.update(name for name in pool_view if isinstance(name, str))
    names.update(pool_for_role(role) for _front, role, _n in allocations)
    names.update(out_now)
    names.update(pool for pool, n in running_pool.items() if n)
    names.update(reserved_pool)

    settings = config.load()

    waiting = waiting_by_pool()
    lines: list[str] = []
    for pool in sorted(names):
        record = out_now.get(pool)
        if record is not None:
            until = _parse_iso(record.get("out_until"))
            because = record.get("because") or "quota"
            when = format_out_until(until) if until is not None else ""
            lines.append(f"  {pool}: out until {when} ({because})")
            continue
        total = settings.cap(pool)
        held = held_pool.get(pool, 0)
        running = running_pool.get(pool, 0)
        reserved = reserved_pool.get(pool, 0)
        if total is None and held == 0 and running == 0 and reserved == 0:
            # Nothing configured and nothing held: an empty row about a
            # pool nobody is using is noise on a one-screen panel.
            continue
        if reserved:
            cap_shown = total if total is not None else "-"
            line = (f"  {pool}: held {held} / reserved {reserved} / "
                    f"cap {cap_shown}")
        else:
            line = (f"  {pool}: {held}/"
                    f"{total if total is not None else '-'} held")
        if running != held:
            line += f" · {running} running on the box"
        # A queued job is waiting for a slot only where there is no slot to
        # give it. With one free, the job waits on the supervisor's order,
        # which the Job queue block already shows by name.
        behind = waiting.get(pool, 0)
        if behind and total is not None and held >= total:
            line += f" · {behind} waiting"
        lines.append(line)
    front_lines: list[tuple[str, str, str]] = []
    for (front, role), count in reserved_front_role.items():
        shown = allocation_out(role, now)
        if shown is not None:
            text = f"  {front} {role}: {shown}"
        else:
            text = (f"  {front} {role}: reserved {count}, "
                    f"running {held_front.get((front, role), 0)}")
        front_lines.append((front, role, text))
    for front, role, limit in allocations:
        if front in holding:
            continue
        shown = allocation_out(role, now)
        if shown is not None:
            text = f"  {front} {role}: {shown}"
        else:
            text = (f"  {front} {role}: "
                    f"{held_front.get((front, role), 0)}/{limit} held")
        front_lines.append((front, role, text))
    for _front, _role, text in sorted(front_lines):
        lines.append(text)
    if not lines:
        return ["Capacity: no collector data yet."]
    return ["Capacity:"] + lines
