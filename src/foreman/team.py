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

from . import config, paths, store


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
