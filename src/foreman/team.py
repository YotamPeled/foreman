"""Working-team derivation from a front's map and tree (decision 31).

The owner's team line is the ceiling. ``derive_team`` applies the
calibration rules: builders = min(independently verifiable leaves, the
supervisor's verification rate, shared-resource slots), muse only when
the mechanical share meets the threshold, reviewers only for behaviour
nodes. Counts never exceed the owner's line.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config, entities, ids, paths, store


#: Two builders per supervisor on one codebase. Three or four only where
#: leaves were disjoint by construction; six on a research front of
#: independent lines (docs/knowledge/team-calibration.md).
VERIFICATION_RATE = 2
VERIFICATION_RATE_DISJOINT = 4
VERIFICATION_RATE_RESEARCH = 6
#: Independently verifiable leaves at which the rate rises.
_DISJOINT_LEAVES = 6
_RESEARCH_LEAVES = 16

#: Muse takes mechanical leaves only when this share of the next
#: milestone's leaves is mechanical. Below it, muse is 0.
MUSE_MECHANICAL_SHARE = 0.5

#: A front with no tree yet: its supervisor and one builder.
NO_TREE_BUILDERS = 1

SIGNAL_POOL_OUT = "pool out"
SIGNAL_MILESTONE_LATE = "milestone late"
SIGNAL_JOBS_FAST = "jobs returning under fifteen minutes"
SIGNAL_FAILURE_TWICE = "same failure class twice"
FAST_JOB_SECONDS = 15 * 60
_BUILDER_ROLES = ("builder", "backup-builder")


@dataclass(frozen=True)
class TeamEntry:
    """One derived working-team slot: a role on a pool, under a ceiling."""

    role: str
    pool: str
    count: int
    ceiling: int
    reason: str
    agent: str = ""
    model: str = ""
    effort: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TeamEntry":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: data[k] for k in data if k in known})


def format_team_line(entry: TeamEntry | dict) -> str:
    """``builder grok: 2 of 3 (14 leaves in the next milestone)``."""
    if isinstance(entry, TeamEntry):
        role, pool = entry.role, entry.pool
        count, ceiling, reason = entry.count, entry.ceiling, entry.reason
    else:
        role = str(entry.get("role") or "")
        pool = str(entry.get("pool") or "")
        count = entry.get("count") if isinstance(entry.get("count"), int) else 0
        ceiling = (entry.get("ceiling")
                   if isinstance(entry.get("ceiling"), int) else 0)
        reason = str(entry.get("reason") or "")
    return f"{role} {pool}: {count} of {ceiling} ({reason})"


def load_tree(front: str) -> list[dict]:
    try:
        return store.fold_by_id(store.read_ledger(paths.front_tree_path(front)))
    except OSError:
        return []


def load_facts(front: str) -> list[dict]:
    try:
        return store.fold_by_id(store.read_ledger(paths.front_map_path(front)))
    except OSError:
        return []


def work_leaves(tree: list[dict]) -> list[dict]:
    """Job nodes that are work, not landing/rebase/front-landing scripts."""
    leaves: list[dict] = []
    for node in tree:
        if not isinstance(node, dict):
            continue
        if node.get("kind") != "job":
            continue
        if node.get("lands"):
            continue
        leaves.append(node)
    return leaves


def _by_id(tree: list[dict]) -> dict[str, dict]:
    return {node["id"]: node for node in tree
            if isinstance(node, dict) and isinstance(node.get("id"), str)}


def _has_ancestor(node: dict, milestone_id: str, by_id: dict[str, dict]
                  ) -> bool:
    seen: set[str] = set()
    current: dict | None = node
    while current is not None:
        nid = current.get("id")
        if nid == milestone_id:
            return True
        parent = current.get("parent")
        if not isinstance(parent, str) or not parent or parent in seen:
            return False
        seen.add(parent)
        if parent == milestone_id:
            return True
        current = by_id.get(parent)
    return False


def _open_milestones(tree: list[dict]) -> list[dict]:
    return [node for node in tree
            if isinstance(node, dict) and node.get("kind") == "milestone"
            and str(node.get("state") or "") != "landed"]


def next_milestone_leaves(tree: list[dict]) -> list[dict]:
    """Job leaves under the next open milestone, or every work leaf."""
    leaves = work_leaves(tree)
    if not leaves:
        return []
    by_id = _by_id(tree)
    open_ms = _open_milestones(tree)
    if not open_ms:
        return leaves
    target = open_ms[0]
    tid = target.get("id")
    if not isinstance(tid, str) or not tid:
        return leaves
    under = [leaf for leaf in leaves if _has_ancestor(leaf, tid, by_id)]
    return under if under else leaves


def _independent_leaves(leaves: list[dict], facts: list[dict]) -> list[dict]:
    """Leaves that can run in parallel.

    Assumed map facts run alone and first: those leaves do not add
    parallel capacity beyond one. Empty must-not-touch is unspecified,
    not shared, so the tests' bare leaves stay independent.
    """
    assumed = [fact for fact in facts
               if isinstance(fact, dict) and fact.get("basis") == "assumed"]
    if not assumed:
        return list(leaves)
    assumed_ids = {str(fact.get("id") or "") for fact in assumed
                   if fact.get("id")}
    resting: list[dict] = []
    free: list[dict] = []
    for leaf in leaves:
        after = leaf.get("after") if isinstance(leaf.get("after"), list) else []
        refs = {str(item) for item in after}
        if assumed_ids and refs & assumed_ids:
            resting.append(leaf)
        else:
            free.append(leaf)
    if resting:
        return free + resting[:1]
    return free


def _verification_rate(independent: int) -> int:
    if independent >= _RESEARCH_LEAVES:
        return VERIFICATION_RATE_RESEARCH
    if independent >= _DISJOINT_LEAVES:
        return VERIFICATION_RATE_DISJOINT
    return VERIFICATION_RATE


def _shared_slots(leaves: list[dict]) -> int | None:
    names: list[str] = []
    seen: set[str] = set()
    for leaf in leaves:
        raw = leaf.get("resources")
        if not isinstance(raw, list):
            continue
        for item in raw:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            names.append(text)
    if not names:
        return None
    settings = config.load()
    counts = []
    for name in names:
        count = settings.resource_count(name)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            counts.append(count)
    return min(counts) if counts else None


def _builder_count(leaves: list[dict], facts: list[dict]) -> tuple[int, int]:
    """(wanted builders, independently verifiable leaf count)."""
    independent = _independent_leaves(leaves, facts)
    n = len(independent)
    wanted = min(n, _verification_rate(n))
    slots = _shared_slots(leaves)
    if slots is not None:
        wanted = min(wanted, slots)
    return wanted, n


def _is_muse(entry: dict) -> bool:
    pool = str(entry.get("pool") or "")
    agent = str(entry.get("agent") or "")
    return pool == "muse" or agent.startswith("muse")


def _mechanical_share(leaves: list[dict]) -> float:
    if not leaves:
        return 0.0
    mechanical = sum(1 for leaf in leaves if leaf.get("mechanical"))
    return mechanical / len(leaves)


def _reason_leaves(n: int) -> str:
    word = "leaf" if n == 1 else "leaves"
    return f"{n} {word} in the next milestone"


def derive_team(record: dict, tree: list[dict], facts: list[dict]
                ) -> list[TeamEntry]:
    """Apply decision 31 to ``record``'s owner team, ``tree`` and ``facts``.

    Each entry's ``count`` is at most its ``ceiling``. A front with no
    work leaves keeps its supervisor and one builder.
    """
    team = record.get("team")
    team = team if isinstance(team, list) else []
    owner: list[dict] = [entry for entry in team if isinstance(entry, dict)]
    if not owner:
        return []
    leaves = next_milestone_leaves(tree)
    has_tree = bool(leaves)
    wanted, leaf_n = _builder_count(leaves, facts)
    share = _mechanical_share(leaves)
    behaviour = [leaf for leaf in leaves if not leaf.get("mechanical")]
    first_builder_used = False
    entries: list[TeamEntry] = []
    for entry in owner:
        role = str(entry.get("role") or "")
        pool = str(entry.get("pool") or "")
        ceiling_raw = entry.get("count")
        ceiling = (ceiling_raw if isinstance(ceiling_raw, int)
                   and not isinstance(ceiling_raw, bool) and ceiling_raw > 0
                   else 0)
        agent = str(entry.get("agent") or "")
        model = str(entry.get("model") or "")
        effort = str(entry.get("effort") or "")
        count = 0
        reason = ""
        if role == "supervisor":
            count = min(1, ceiling) if ceiling else 1
            if ceiling < 1:
                ceiling = count
            reason = "supervisor"
        elif role == "builder" and _is_muse(entry):
            if not has_tree or share < MUSE_MECHANICAL_SHARE:
                count = 0
                reason = "mechanical share under threshold"
            else:
                mechanical = [leaf for leaf in leaves if leaf.get("mechanical")]
                muse_wanted, _n = _builder_count(mechanical, facts)
                count = min(muse_wanted, ceiling)
                reason = _reason_leaves(len(mechanical))
        elif role == "builder":
            if not has_tree:
                if not first_builder_used:
                    count = min(NO_TREE_BUILDERS, ceiling)
                    first_builder_used = True
                    reason = "no tree yet"
                else:
                    count = 0
                    reason = "no tree yet"
            else:
                count = min(wanted, ceiling)
                reason = _reason_leaves(leaf_n)
        elif role == "backup-builder":
            if not has_tree:
                count = 0
                reason = "no tree yet"
            else:
                count = min(1, ceiling)
                reason = "backup after a failed builder run"
        elif role == "reviewer":
            if behaviour:
                count = min(1, ceiling)
                reason = "review load for behaviour nodes"
            else:
                count = 0
                reason = "no behaviour nodes"
        else:
            count = 0
            reason = "unknown role"
        if count > ceiling and ceiling > 0:
            count = ceiling
        entries.append(TeamEntry(
            role=role, pool=pool, count=count, ceiling=ceiling,
            reason=reason, agent=agent, model=model, effort=effort,
        ))
    return entries


def record_working_team(name: str, record: dict | None = None, *,
                        tree: list[dict] | None = None,
                        facts: list[dict] | None = None,
                        who: str | None = None) -> dict | None:
    """Write ``working_team`` and ``derived_at`` on the front record."""
    from . import fronts

    key = (name or "").strip()
    if not key:
        return record
    current = record if record is not None else fronts.read_front_record(key)
    if current is None:
        return None
    nodes = tree if tree is not None else load_tree(key)
    map_facts = facts if facts is not None else load_facts(key)
    entries = derive_team(current, nodes, map_facts)
    payload = [entry.to_dict() for entry in entries]
    return fronts.revise_front(
        key, current, who,
        working_team=payload,
        derived_at=store.utcnow_iso(),
    )


def working_team_of(record: dict, tree: list[dict] | None = None,
                    facts: list[dict] | None = None) -> list[dict]:
    """Recorded working team, or a live derivation when none is stored."""
    stored = record.get("working_team")
    if isinstance(stored, list) and stored:
        return [entry for entry in stored if isinstance(entry, dict)]
    name = str(record.get("name") or "")
    nodes = tree if tree is not None else load_tree(name)
    map_facts = facts if facts is not None else load_facts(name)
    return [entry.to_dict() for entry in derive_team(record, nodes, map_facts)]


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


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _evidence_claims(front: str) -> list[str]:
    try:
        lines = store.read_ledger(paths.front_evidence_path(front))
    except OSError:
        return []
    claims: list[str] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        claim = line.get("claim")
        if isinstance(claim, str) and claim:
            claims.append(claim)
    return claims


def _already_noted(front: str, signal: str) -> bool:
    needle = f"({signal})"
    return any(needle in claim for claim in _evidence_claims(front))


def _open_asks(front: str) -> list[dict]:
    try:
        items = store.fold_by_id(store.read_ledger(paths.inbox_path()))
    except OSError:
        return []
    found: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or item.get("answered_at"):
            continue
        question = str(item.get("question") or "")
        if f"front {front} " in question or question.startswith(f"front {front}"):
            found.append(item)
    return found


def _milestone_late(front: str, now: datetime) -> bool:
    moment = _as_utc(now)
    nodes = load_tree(front)
    try:
        milestones = store.fold_by_id(
            store.read_ledger(paths.front_milestones_path(front)))
    except OSError:
        milestones = []
    rows = list(milestones) + [
        node for node in nodes
        if isinstance(node, dict) and node.get("kind") == "milestone"]
    for row in rows:
        if str(row.get("state") or "") == "landed":
            continue
        estimate = _parse_iso(row.get("estimate") or row.get("eta"))
        if estimate is not None and moment > estimate:
            return True
    return False


def _jobs_fast(front: str) -> bool:
    try:
        jobs = store.fold_by_id(store.read_ledger(paths.front_jobs_path(front)))
    except OSError:
        return False
    for job in jobs:
        if not isinstance(job, dict):
            continue
        started = _parse_iso(job.get("started_at"))
        returned = _parse_iso(job.get("returned_at"))
        if started is None or returned is None:
            continue
        if (returned - started).total_seconds() < FAST_JOB_SECONDS:
            return True
    return False


def _failure_twice(front: str) -> bool:
    try:
        findings = store.read_ledger(paths.front_findings_path(front))
    except OSError:
        findings = []
    counts: dict[str, int] = {}
    for line in findings:
        if not isinstance(line, dict):
            continue
        class_ = line.get("class")
        if not (isinstance(class_, str) and class_):
            continue
        counts[class_] = counts.get(class_, 0) + 1
        if counts[class_] >= 2:
            return True
    try:
        jobs = store.fold_by_id(store.read_ledger(paths.front_jobs_path(front)))
    except OSError:
        jobs = []
    review_counts: dict[str, int] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        class_ = job.get("review_class")
        if not (isinstance(class_, str) and class_) or class_ == "clean":
            continue
        review_counts[class_] = review_counts.get(class_, 0) + 1
        if review_counts[class_] >= 2:
            return True
    return False


def detect_signals(front: str, record: dict,
                   now: datetime | None = None) -> list[str]:
    """Signals that fire on this front at ``now``, in a stable order."""
    from . import capacity

    moment = _as_utc(now)
    fired: list[str] = []
    team = working_team_of(record)
    our_pools = {str(entry.get("pool") or "") for entry in team
                 if entry.get("role") in _BUILDER_ROLES
                 and entry.get("pool")}
    out = set(capacity.out_pools(moment))
    if our_pools & out:
        fired.append(SIGNAL_POOL_OUT)
    if _milestone_late(front, moment):
        fired.append(SIGNAL_MILESTONE_LATE)
    if _jobs_fast(front):
        fired.append(SIGNAL_JOBS_FAST)
    if _failure_twice(front):
        fired.append(SIGNAL_FAILURE_TWICE)
    return fired


def _builder_indexes(working: list[dict], *,
                     avoid_pools: set[str] | None = None) -> list[int]:
    avoid = avoid_pools or set()
    indexes: list[int] = []
    for i, entry in enumerate(working):
        if entry.get("role") not in _BUILDER_ROLES:
            continue
        pool = str(entry.get("pool") or "")
        if pool in avoid:
            continue
        indexes.append(i)
    return indexes


def _raise_index(working: list[dict], *,
                 avoid_pools: set[str] | None = None) -> int | None:
    """First builder under its ceiling, else None (at the ceiling)."""
    for i in _builder_indexes(working, avoid_pools=avoid_pools):
        count = _int(working[i].get("count"))
        ceiling = _int(working[i].get("ceiling"))
        if count < ceiling:
            return i
    return None


def _ask_index(working: list[dict], *,
               avoid_pools: set[str] | None = None) -> int | None:
    indexes = _builder_indexes(working, avoid_pools=avoid_pools)
    return indexes[0] if indexes else None


def _lower_index(working: list[dict]) -> int | None:
    for i in _builder_indexes(working):
        if _int(working[i].get("count")) > 0:
            return i
    return None


def file_ceiling_ask(front: str, entry: dict, signal: str,
                     who: str | None) -> None:
    """File an owner ask with the numbers (tsk-5.4's inbox path).

    The raise itself is tsk-5.4: this only asks, and only when no open
    ask for this front and signal is already sitting in the inbox.
    """
    marker = f"({signal})"
    for item in _open_asks(front):
        if marker in str(item.get("question") or ""):
            return
    role = str(entry.get("role") or "")
    pool = str(entry.get("pool") or "")
    count = _int(entry.get("count"))
    ceiling = _int(entry.get("ceiling"))
    want = count + 1
    store.append_ledger(
        paths.inbox_path(),
        entities.InboxItem(
            id=ids.mint("inbox"),
            from_=who or "foreman",
            kind="money",
            question=(
                f"front {front} wants {role} {pool} {want} "
                f"(ceiling {ceiling}) {marker}"
            ),
            recommendation=f"raise {pool} on {front} to {want}",
            asked_at=store.utcnow_iso(),
        ).to_dict(),
        session_id=who,
    )


def _write_adjustment(front: str, entry: dict, old: int, new: int,
                      signal: str, who: str | None) -> None:
    role = str(entry.get("role") or "")
    pool = str(entry.get("pool") or "")
    store.append_ledger(
        paths.front_evidence_path(front),
        entities.Evidence(
            id=ids.mint("evidence"),
            on=front,
            claim=(f"team adjusted: {role} {pool} {old} -> {new} "
                   f"({signal})"),
            status="CONFIRMED",
            command="collector tick",
        ).to_dict(),
        session_id=who,
    )


def apply_team_signals(name: str, record: dict | None = None, *,
                       now: datetime | None = None,
                       who: str | None = None) -> dict | None:
    """Move the working team one step per new signal, or file an ask."""
    from . import capacity
    from . import fronts

    key = (name or "").strip()
    if not key:
        return record
    current = record if record is not None else fronts.read_front_record(key)
    if current is None:
        return None
    moment = _as_utc(now)
    subject = who or "collector"
    working = [dict(entry) for entry in working_team_of(current)]
    if not working:
        return current
    changed = False
    for signal in detect_signals(key, current, moment):
        if _already_noted(key, signal):
            continue
        avoid: set[str] = set()
        if signal == SIGNAL_POOL_OUT:
            avoid = {pool for pool, _rec in capacity.out_pools(moment).items()}
        if signal == SIGNAL_JOBS_FAST:
            idx = _lower_index(working)
            if idx is None:
                continue
            old = _int(working[idx].get("count"))
            new = old - 1
            working[idx]["count"] = new
            _write_adjustment(key, working[idx], old, new, signal, subject)
            changed = True
            continue
        idx = _raise_index(working, avoid_pools=avoid)
        if idx is None:
            ask_at = _ask_index(working, avoid_pools=avoid)
            if ask_at is not None:
                file_ceiling_ask(key, working[ask_at], signal, subject)
            continue
        old = _int(working[idx].get("count"))
        new = old + 1
        working[idx]["count"] = new
        _write_adjustment(key, working[idx], old, new, signal, subject)
        changed = True
    if not changed:
        return current
    return fronts.revise_front(
        key, current, subject,
        working_team=working,
        derived_at=store.utcnow_iso(),
    )


def adjust_all_fronts(now: datetime | None = None,
                      who: str | None = None) -> None:
    """Read the ledgers and adjust every live v5 front (one collector tick)."""
    from . import fronts

    try:
        names = sorted(entry.name for entry in paths.fronts_dir().iterdir()
                       if entry.is_dir())
    except OSError:
        return
    moment = _as_utc(now)
    subject = who or "collector"
    for name in names:
        record = fronts.read_front_record(name)
        if record is None or record.get("shape") != "v5":
            continue
        if record.get("state") in ("done", "halted", "frozen", "stopped"):
            continue
        apply_team_signals(name, record, now=moment, who=subject)
