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
limit on one model family and is shared first come, first served. A
front's **allocation** is a ceiling per role — the most that front may hold
at once — and never a reservation: an idle allocation holds nothing and
another front may take the slot (docs/DESIGN.md section 3, section 9 rule
12). Both are checked at the moment of a grant and never before.
"""

from __future__ import annotations

from . import config, entities, fronts, ids, paths, store

#: Work not yet running: a job in one of these states is waiting for a slot.
QUEUE_STATES = ("planned", "queued")


def pool_for_role(role: str) -> str:
    """The pool a worker role runs on. A worker role names its own pool,
    which is what ``launch``'s own default does; only the two interactive
    roles are named apart from theirs."""
    return role


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


def grant(*, pool: str, front: str | None, role: str, job: str | None,
          session: str | None) -> dict:
    """Take one slot for a job that is starting. Appended, never counted."""
    record = entities.SlotGrant(
        id=ids.mint("slot"), pool=pool, front=front or "", role=role,
        job=job, session=session, granted_at=store.utcnow_iso(),
        released_at=None, released_because="",
    )
    return store.append_ledger(paths.slots_path(), record.to_dict(),
                               session_id=session)


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


def ceiling(front: str | None, role: str) -> int | None:
    """The most sessions ``front`` may hold of ``role``, or None for none.

    None means "no ceiling at all", and only a front with no record on the
    ledger gets it: nothing said what that front may hold, so only the pool
    cap speaks. A front that does have a record is held to it, and a role
    its allocation does not name is allocated nothing.
    """
    if not front:
        return None
    record = fronts.read_front_record(front)
    if record is None:
        return None
    allocation = record.get("allocation")
    value = allocation.get(role) if isinstance(allocation, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def launch_problems(role: str, pool: str, front: str | None) -> list[str]:
    """The capacity refusals for one launch, in the order they are checked.

    The front's ceiling first, then the pool's cap, each naming the numbers
    it saw and the limit it hit. Both are returned so a launch wrong on
    both counts is told both at once, like every other refusal here.
    """
    problems: list[str] = []
    limit = ceiling(front, role)
    if limit is not None:
        held = held_by_front_role().get((front or "", role), 0)
        if held >= limit:
            problems.append(f"role {role!r} on front {front!r}: "
                            f"{held} held, ceiling {limit}")
    cap = config.load().cap(pool)
    if cap is not None:
        held = held_by_pool().get(pool, 0)
        if held >= cap:
            problems.append(f"pool {pool!r}: {held} held, cap {cap}")
    return problems


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
    """(front, role, ceiling) for every role a front record allocates."""
    rows: list[tuple[str, str, int]] = []
    try:
        names = sorted(entry.name for entry in paths.fronts_dir().iterdir()
                       if entry.is_dir())
    except OSError:
        return rows
    for name in names:
        record = fronts.read_front_record(name)
        allocation = (record or {}).get("allocation")
        if not isinstance(allocation, dict):
            continue
        for role in sorted(allocation):
            value = allocation[role]
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            rows.append((name, role, value))
    return rows


def capacity_lines(observed: dict | None) -> list[str]:
    """The Capacity block: held/cap per pool, held/ceiling per front role,
    and who is waiting on a pool that has nothing left to give.

    Held comes from the slot ledger, so the block is right whether or not
    the collector has ticked. A pool's cap comes from the collector's total
    where it has one and from the configuration otherwise — the two are the
    same number, and reading the collector's copy first keeps this screen
    from opening the configuration to answer a question the tick already
    answered.
    """
    pool_view = (observed or {}).get("pools")
    pool_view = pool_view if isinstance(pool_view, dict) else {}
    held_pool = held_by_pool()
    allocations = front_allocations()
    held_front = held_by_front_role()

    names = set(held_pool)
    names.update(name for name in pool_view if isinstance(name, str))
    names.update(pool_for_role(role) for _front, role, _n in allocations)

    def observed_total(pool: str):
        entry = pool_view.get(pool)
        total = entry.get("total") if isinstance(entry, dict) else None
        return total if isinstance(total, int) and not isinstance(
            total, bool) else None

    # The configuration is opened only for a pool the collector has no
    # total for: a status run against a state directory alone must not
    # reach into the machine's config to render a block it can already
    # answer.
    settings = None
    if any(observed_total(pool) is None for pool in names):
        settings = config.load()

    waiting = waiting_by_pool()
    lines: list[str] = []
    for pool in sorted(names):
        total = observed_total(pool)
        if total is None and settings is not None:
            total = settings.cap(pool)
        held = held_pool.get(pool, 0)
        if total is None and held == 0:
            # Nothing configured and nothing held: an empty row about a
            # pool nobody is using is noise on a one-screen panel.
            continue
        line = f"  {pool}: {held}/{total if total is not None else '-'} held"
        # A queued job is waiting for a slot only where there is no slot to
        # give it. With one free, the job waits on the supervisor's order,
        # which the Job queue block already shows by name.
        behind = waiting.get(pool, 0)
        if behind and total is not None and held >= total:
            line += f" · {behind} waiting"
        lines.append(line)
    for front, role, limit in allocations:
        lines.append(f"  {front} {role}: "
                     f"{held_front.get((front, role), 0)}/{limit} held")
    if not lines:
        return ["Capacity: no collector data yet."]
    return ["Capacity:"] + lines
