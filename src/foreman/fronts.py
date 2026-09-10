"""`foreman front add|list|show|team|queue|prefer|allocate|reserve|release|close`: a brief becomes a front on the ledger.

``front add <dir>`` reads ``<dir>/brief.toml`` with :mod:`tomllib`, refuses
with every violation named at once (docs/DESIGN.md section 4.3), and otherwise
appends one front line to ``fronts/<name>/front.jsonl`` plus one line per task
to ``fronts/<name>/tasks.jsonl``, then copies the brief (and ``plan.md`` when
the directory has one) beside the config so the front no longer depends on the
directory it came from. ``front prefer``, ``front allocate`` and ``front close`` never edit: they
append a revised copy of the front line and readers fold last-wins, exactly
like every other ledger in the state directory. ``front reserve`` and
``front release`` write ``reservations.jsonl`` the same way: an open line
holds the front's team against the pool cap until a revised copy carries
``released_at``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from . import caller, cli, config, entities, ids, monitors, paths, store
from .pools import plugins as pool_plugins
from .caller import Refusal
from .entities import JOB_ROLES
from .team import (  # noqa: F401 - re-export for the job's named seam
    TeamEntry, apply_team_signals, derive_team, format_team_line,
    record_working_team,
)

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

#: The four headings every task scope must carry (docs/DESIGN.md section 4.2).
_SCOPE_PARTS = ("WHAT", "INPUTS", "OUTPUTS", "OUT OF SCOPE")

_SENTENCE_ENDS = (".", "!", "?")

#: An alert is a comparison operator and a number: `< 0.80`, `>= 5`, `!= 0`.
_ALERT_RE = re.compile(
    r"^\s*(?:<=|>=|==|!=|<|>)\s*[+-]?"
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*$")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_one_sentence(text: str) -> bool:
    """One sentence: a single line with one sentence boundary in it.

    A boundary is a terminator followed by whitespace or the end of the line,
    so file names like ``briefs/panel/brief.toml`` do not read as three
    sentences. The line must end on its boundary.
    """
    stripped = text.strip()
    if not stripped or "\n" in stripped:
        return False
    if not stripped.endswith(_SENTENCE_ENDS):
        return False
    return len(re.findall(r"[.!?](?=\s|$)", stripped)) == 1


#: A brief that carries ``goal`` is v5-shape: different required keys,
#: and the v1 keys it replaces are violations rather than inputs.
_V5_EFFORTS = ("low", "medium", "high", "xhigh")
_V5_TEAM_ROLES = ("supervisor", "builder", "backup-builder", "reviewer")
_V5_LAND = ("push", "pr")
_V5_REPO_REQUIRED = ("name", "url", "base", "work", "target", "check")
#: v1 key → the v5 key that replaces it. ``task`` has no v5 brief key
#: (jobs come from the tree); the violation still names ``[[task]]``.
_V5_REPLACED = {
    "want": "goal",
    "done-when": "finish-line",
    "land-on": "[[repository]]",
    "task": "[[task]]",
    "allocation": "team",
}


def _is_v5_brief(data: dict) -> bool:
    """True when the brief carries ``goal``, the v5-shape marker."""
    return "goal" in data


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_v5(data: dict, *,
                 allow_existing_work: bool = False) -> list[str]:
    """Every v5-shape violation at once, each naming the field it breaks."""
    violations: list[str] = []

    for old, new in _V5_REPLACED.items():
        if old in data:
            if old == "task":
                violations.append(
                    "field 'task' is a v1 key; a v5 brief uses no '[[task]]'")
            else:
                violations.append(
                    f"field '{old}' is a v1 key; a v5 brief uses '{new}'")

    goal = data.get("goal")
    if not _nonempty_str(goal):
        violations.append("field 'goal' is required (a non-empty string)")

    finish = data.get("finish-line")
    if finish is None or (isinstance(finish, str) and not finish.strip()):
        violations.append(
            "field 'finish-line' is required (a non-empty string)")
    elif not isinstance(finish, str) or not _is_one_sentence(finish):
        violations.append("field 'finish-line' must be one sentence")

    decisions = data.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        violations.append(
            "field 'decisions' is required (a non-empty list of "
            "non-empty strings)")
    else:
        for index, item in enumerate(decisions):
            if not _nonempty_str(item):
                violations.append(
                    f"field 'decisions' #{index + 1} must be a "
                    f"non-empty string")

    supervisor = data.get("supervisor")
    if not _nonempty_str(supervisor):
        violations.append(
            "field 'supervisor' is required ('<agent>:<effort>')")
    else:
        violations.extend(
            _v5_supervisor_violations(supervisor.strip(), "field 'supervisor'"))

    team = data.get("team")
    if team is None:
        violations.append(
            "field 'team' is required (a list of "
            "'<agent>:<effort>:<count>:<role>')")
    elif not isinstance(team, list):
        violations.append(
            "field 'team' must be a list of "
            "'<agent>:<effort>:<count>:<role>'")
    else:
        for index, entry in enumerate(team):
            tag = f"field 'team' #{index + 1}"
            violations.extend(_v5_team_violations(entry, tag))

    repos = data.get("repository")
    if not isinstance(repos, list) or not repos:
        violations.append(
            "field 'repository' is required (one or more tables)")
    else:
        for index, repo in enumerate(repos):
            tag = f"repository #{index + 1}"
            if not isinstance(repo, dict):
                violations.append(f"{tag} must be a table")
                continue
            name = repo.get("name")
            if _nonempty_str(name):
                tag = f"repository '{name.strip()}'"
            violations.extend(_v5_repository_violations(
                repo, tag, allow_existing_work=allow_existing_work))
    return violations


def _v5_unknown_agent(agent: str, tag: str) -> str | None:
    if pool_plugins.resolve_agent(agent) is not None:
        return None
    return (f"{tag} unknown agent '{agent}'; "
            f"{pool_plugins.known_agents_clause()}")


def _v5_supervisor_violations(value: str, tag: str) -> list[str]:
    parts = value.split(":")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return [f"{tag} must be '<agent>:<effort>'"]
    agent, effort = parts
    violations: list[str] = []
    if effort not in _V5_EFFORTS:
        violations.append(
            f"{tag} effort must be {', '.join(_V5_EFFORTS)} "
            f"(got '{effort}')")
    unknown = _v5_unknown_agent(agent, tag)
    if unknown is not None:
        violations.append(unknown)
    return violations


def _v5_team_violations(entry: object, tag: str) -> list[str]:
    if not isinstance(entry, str) or not entry.strip():
        return [f"{tag} must be '<agent>:<effort>:<count>:<role>'"]
    parts = entry.strip().split(":")
    if len(parts) != 4 or not parts[0]:
        return [f"{tag} must be '<agent>:<effort>:<count>:<role>'"]
    agent, effort, count, role = parts
    violations: list[str] = []
    if effort not in _V5_EFFORTS:
        violations.append(
            f"{tag} effort must be {', '.join(_V5_EFFORTS)} "
            f"(got '{effort}')")
    if not count or not count.isdigit() or int(count) < 1:
        violations.append(
            f"{tag} count must be a positive integer (got '{count}')")
    if role not in _V5_TEAM_ROLES:
        violations.append(
            f"{tag} role must be {', '.join(_V5_TEAM_ROLES)} "
            f"(got '{role}')")
    unknown = _v5_unknown_agent(agent, tag)
    if unknown is not None:
        violations.append(unknown)
    return violations


def _v5_repository_violations(repo: dict, tag: str, *,
                              allow_existing_work: bool = False
                              ) -> list[str]:
    violations: list[str] = []
    for key in _V5_REPO_REQUIRED:
        value = repo.get(key)
        if not _nonempty_str(value):
            violations.append(f"{tag}: field '{key}' is required")
    land = repo.get("land")
    if land is not None and land not in _V5_LAND:
        violations.append(
            f"{tag}: field 'land' must be 'push' or 'pr' "
            f"(got '{land}')")
    if "trailers" in repo:
        trailers = repo.get("trailers")
        if not isinstance(trailers, list) or any(
                not isinstance(item, str) for item in trailers):
            violations.append(
                f"{tag}: field 'trailers' must be a list of strings")
    if "pr-body" in repo and not isinstance(repo.get("pr-body"), str):
        violations.append(f"{tag}: field 'pr-body' must be a string")
    url = repo.get("url")
    if _nonempty_str(url):
        violations.extend(_v5_remote_violations(
            url.strip(), repo, tag, allow_existing_work=allow_existing_work))
    return violations


def _v5_remote_violations(url: str, repo: dict, tag: str, *,
                          allow_existing_work: bool = False
                          ) -> list[str]:
    """Refuse a work branch that already exists, or a missing base/target.

    ``allow_existing_work`` is the adopt path: the work branch is the
    front's own, so its presence on the remote is expected, not a
    violation. Base/target checks are unchanged.
    """
    violations: list[str] = []
    work = repo.get("work")
    if _nonempty_str(work):
        sha, err = _ls_remote(url, work.strip())
        if err is not None:
            violations.append(
                f"{tag}: field 'work' cannot be checked: {err}")
        elif sha is not None and not allow_existing_work:
            violations.append(
                f"{tag}: field 'work' branch '{work.strip()}' "
                f"exists on the remote")
    for key in ("base", "target"):
        branch = repo.get(key)
        if not _nonempty_str(branch):
            continue
        sha, err = _ls_remote(url, branch.strip())
        if err is not None:
            violations.append(
                f"{tag}: field '{key}' cannot be checked: {err}")
        elif sha is None:
            violations.append(
                f"{tag}: field '{key}' branch '{branch.strip()}' "
                f"is absent on the remote")
    return violations


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """A task-after cycle as a title path, or None when the graph is clean."""
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> list[str] | None:
        visiting.append(node)
        for nxt in graph[node]:
            if nxt not in graph:
                continue
            if nxt in visiting:
                return visiting[visiting.index(nxt):] + [nxt]
            if nxt not in done:
                hit = visit(nxt)
                if hit is not None:
                    return hit
        visiting.pop()
        done.add(node)
        return None

    for node in graph:
        if node not in done:
            hit = visit(node)
            if hit is not None:
                return hit
    return None


def read_front_record(name: str) -> dict | None:
    """The folded front line for an added front; None when it has no record."""
    try:
        folded = store.fold_by_id(store.read_ledger(paths.front_record_path(name)))
    except OSError:
        return None
    return folded[-1] if folded else None


#: Team roles that occupy the builders reservation phase.
_BUILDER_TEAM_ROLES = ("builder", "backup-builder")
#: Team roles that occupy the reviewers reservation phase.
_REVIEWER_TEAM_ROLES = ("reviewer",)
#: ``front reserve --phase`` values other than ``all``.
RESERVE_PHASES = ("builders", "reviewers")


def folded_reservations() -> list[dict]:
    """Every reservation, folded last-wins, in first-appearance order."""
    try:
        records = store.read_ledger(paths.reservations_path())
    except OSError:
        return []
    return store.fold_by_id(records)


def open_reservations() -> list[dict]:
    """Reservations that still hold a pool: no ``released_at`` on the fold."""
    return [record for record in folded_reservations()
            if record.get("released_at") is None]


def front_has_open_reservation(front: str) -> bool:
    return any(record.get("front") == front for record in open_reservations())


def _job_reservation_count(record: dict) -> int | None:
    """Count this line holds against a pool cap, or None to skip it.

    A supervisor reservation holds the front's supervisor ceiling; it
    is not a job slot on the pool (``supervisor_cap`` is that limit).
    """
    if record.get("role") == "supervisor":
        return None
    count = record.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count


def reserved_by_pool(*, except_front: str | None = None) -> dict[str, int]:
    """Open reservation counts by pool, optionally skipping one front."""
    held: dict[str, int] = {}
    for record in open_reservations():
        if except_front is not None and record.get("front") == except_front:
            continue
        pool = record.get("pool")
        count = _job_reservation_count(record)
        if not (isinstance(pool, str) and pool) or count is None:
            continue
        held[pool] = held.get(pool, 0) + count
    return held


def reserved_by_front_role() -> dict[tuple[str, str], int]:
    """Open reservation counts keyed by (front, job role)."""
    held: dict[tuple[str, str], int] = {}
    for record in open_reservations():
        front, role = record.get("front"), record.get("role")
        count = record.get("count")
        if not (isinstance(front, str) and front
                and isinstance(role, str) and role):
            continue
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            continue
        held[(front, role)] = held.get((front, role), 0) + count
    return held


def reserved_fronts() -> set[str]:
    """Front names that currently hold at least one open reservation."""
    names: set[str] = set()
    for record in open_reservations():
        front = record.get("front")
        if isinstance(front, str) and front:
            names.add(front)
    return names


def _pool_cap(pool: str) -> int | None:
    """The live cap for ``pool``: a quota-ask raise, else the configuration."""
    from . import capacity as capacity_mod

    return capacity_mod.effective_cap(pool)


def builders_misfit(record: dict) -> tuple[str, int, int, list[str], int] | None:
    """The first builders-phase pool that does not fit, or None.

    Returns ``(pool, cap, reserved, others, wants)``. A pool with no cap
    always fits. Shared with the collector's quota ask and ``team_fits``.
    """
    name = record.get("name")
    name = name if isinstance(name, str) else ""
    wanted = _wanted_reservations(record, ("builders",))
    open_recs = open_reservations()
    for pool, _role, count, _phase in wanted:
        cap = _pool_cap(pool)
        if cap is None:
            continue
        reserved, others = _others_on_pool(open_recs, pool, name)
        if cap - reserved < count:
            return pool, cap, reserved, others, count
    return None


def team_fits(record: dict) -> str | None:
    """None when the builders phase fits; otherwise why it does not.

    Fits when every pool has ``cap - reserved_by_others >= count`` for
    the builders phase. The refusal names the first pool that does not:
    ``pool grok: cap 6, reserved 4, wants 3``. A pool with no cap always
    fits. Shared with the collector's queue tick.
    """
    misfit = builders_misfit(record)
    if misfit is None:
        return None
    pool, cap, reserved, _others, wants = misfit
    return f"pool {pool}: cap {cap}, reserved {reserved}, wants {wants}"
    name = record.get("name")
    name = name if isinstance(name, str) else ""
    wanted = _wanted_reservations(record, ("builders",))
    open_recs = open_reservations()
    settings = config.load()
    for pool, role, count, _phase in wanted:
        if role == "supervisor":
            continue
        cap = settings.cap(pool)
        if cap is None:
            continue
        reserved, _others = _others_on_pool(open_recs, pool, name)
        if cap - reserved < count:
            return (f"pool {pool}: cap {cap}, reserved {reserved}, "
                    f"wants {count}")
    return None


def _front_names() -> list[str]:
    try:
        return [entry.name for entry in paths.fronts_dir().iterdir()
                if entry.is_dir()]
    except OSError:
        return []


def _requested_at(record: dict) -> str:
    """When this front was asked for: ``requested_at``, else the first
    ledger line's ``at`` (later revisions stamp a new ``at``)."""
    value = record.get("requested_at")
    if isinstance(value, str) and value:
        return value
    name = record.get("name")
    if isinstance(name, str) and name:
        try:
            lines = store.read_ledger(paths.front_record_path(name))
        except OSError:
            lines = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            at = line.get("at")
            if isinstance(at, str) and at:
                return at
    at = record.get("at")
    return at if isinstance(at, str) else ""


def _queue_key(record: dict) -> tuple:
    prefer = record.get("prefer", 0)
    if isinstance(prefer, bool) or not isinstance(prefer, int):
        prefer = 0
    return (prefer, _requested_at(record), record.get("name") or "")


def queued_fronts() -> list[dict]:
    """Queued front records in order: ``prefer`` ascending, then
    ``requested_at``."""
    rows: list[dict] = []
    for name in _front_names():
        record = read_front_record(name)
        if record is None or record.get("state") != "queued":
            continue
        rows.append(record)
    rows.sort(key=_queue_key)
    return rows


def queue_wait_reason(record: dict, queued: list[dict]) -> str:
    """Why this queued front waits, as ``front queue`` prints it.

    The top front is ``team fits`` or the pool numbers; when the
    machine has no room to raise, ``no room: cap N is the maximum``.
    Every front below the top is ``behind <top>``.
    """
    if not queued:
        return "team fits"
    top = queued[0]
    top_name = top.get("name") or ""
    if record.get("name") != top_name:
        return f"behind {top_name}"
    misfit = builders_misfit(record)
    if misfit is None:
        return "team fits"
    pool, cap, reserved, _others, wants = misfit
    from . import capacity as capacity_mod

    if not capacity_mod.has_raise_room(pool, cap):
        if capacity_mod.raise_limits(pool):
            return f"no room: cap {cap} is the maximum"
    return f"pool {pool}: cap {cap}, reserved {reserved}, wants {wants}"


def _holder_with_most(open_recs: list[dict], pool: str,
                      except_front: str) -> str | None:
    """The other front holding the most of ``pool``, earliest on a tie."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for record in open_recs:
        if record.get("pool") != pool:
            continue
        front = record.get("front")
        if not isinstance(front, str) or not front or front == except_front:
            continue
        count = record.get("count")
        n = count if isinstance(count, int) and not isinstance(count, bool) else 0
        if n < 0:
            n = 0
        if front not in counts:
            order.append(front)
            counts[front] = 0
        counts[front] += n
    if not counts:
        return None
    return max(order, key=lambda name: (counts[name], -order.index(name)))


def quota_ask_question(name: str, pool: str, cap: int, reserved: int,
                       others: list[str], wants: int, raised: int) -> str:
    """The inbox question for one quota ask."""
    by = f" by {', '.join(others)}" if others else ""
    return (f"front {name} waits on pool {pool}: cap {cap}, "
            f"reserved {reserved}{by}, wants {wants}. "
            f"Raise the cap to {raised} until {name} ends?")


def maybe_file_quota_ask(record: dict, now_iso: str) -> str | None:
    """File one money ask for the top front's first misfit pool.

    Returns the inbox id when a new ask is filed, else None. No ask
    when the machine has no room, or when this front already carries
    a ``quota_ask`` for that pool.
    """
    from . import capacity as capacity_mod

    misfit = builders_misfit(record)
    if misfit is None:
        return None
    pool, cap, reserved, others, wants = misfit
    if not capacity_mod.has_raise_room(pool, cap):
        return None
    name = record.get("name")
    name = name if isinstance(name, str) else ""
    if not name:
        return None
    existing = record.get("quota_ask")
    existing = existing if isinstance(existing, dict) else {}
    if existing.get(pool):
        return None
    raised = reserved + wants
    holder = _holder_with_most(open_reservations(), pool, name)
    options = [f"raise to {raised}", "wait"]
    if holder:
        options.append(f"stop {holder}")
    question = quota_ask_question(
        name, pool, cap, reserved, others, wants, raised)
    iid = ids.mint("inbox")
    store.append_ledger(
        paths.inbox_path(),
        entities.InboxItem(
            id=iid, from_="collector", kind="money",
            question=question, recommendation=f"raise to {raised}",
            options=options, asked_at=now_iso,
        ).to_dict(),
    )
    carried = dict(existing)
    carried[pool] = iid
    revise_front(name, record, "collector", quota_ask=carried)
    return iid


def _phase_team_roles(phase: str) -> tuple[str, ...]:
    if phase == "builders":
        return _BUILDER_TEAM_ROLES
    return _REVIEWER_TEAM_ROLES


def _count_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _wanted_reservations(record: dict,
                         phases: tuple[str, ...]) -> list[tuple[str, str, int, str]]:
    """(pool, job-role, count, phase) this front would reserve.

    v5 fronts walk ``working_team`` when it has been recorded, otherwise
    ``team``: builder and backup-builder are the builders phase,
    reviewer the reviewers phase. A recorded working team also reserves
    its supervisor (a front with no tree yet holds the supervisor and
    one builder). Entries of the same pool, phase and job-role sum. An
    old-shape front reserves its ``allocation`` as the builders phase,
    keyed by the allocation's job role.
    """
    wanted: dict[tuple[str, str, str], int] = {}
    if record.get("shape") == "v5":
        source = record.get("working_team")
        using_working = isinstance(source, list) and any(
            isinstance(entry, dict) for entry in source)
        if not using_working:
            source = record.get("team")
            source = source if isinstance(source, list) else []
        for phase in phases:
            roles = set(_phase_team_roles(phase))
            if using_working and phase == "builders":
                roles.add("supervisor")
            for entry in source:
                if not isinstance(entry, dict):
                    continue
                role = entry.get("role")
                if role not in roles:
                    continue
                pool = entry.get("pool")
                count = _count_int(entry.get("count"))
                if not (isinstance(pool, str) and pool) or count is None:
                    continue
                if role == "supervisor":
                    job_role = "supervisor"
                else:
                    job_role = _job_role_for_pool(pool) or pool
                key = (pool, phase, job_role)
                wanted[key] = wanted.get(key, 0) + count
    elif "builders" in phases:
        allocation = record.get("allocation")
        allocation = allocation if isinstance(allocation, dict) else {}
        for role, raw in allocation.items():
            count = _count_int(raw)
            if not (isinstance(role, str) and role) or count is None:
                continue
            pool = config.load().pool_for_role(role) or role
            key = (pool, "builders", role)
            wanted[key] = wanted.get(key, 0) + count
    return [(pool, job_role, count, phase)
            for (pool, phase, job_role), count in wanted.items()]


def _others_on_pool(open_recs: list[dict], pool: str,
                    except_front: str) -> tuple[int, list[str]]:
    """(count, front names) of open reservations on ``pool`` not this front."""
    total = 0
    names: list[str] = []
    seen: set[str] = set()
    for record in open_recs:
        if record.get("pool") != pool:
            continue
        front = record.get("front")
        if not isinstance(front, str) or not front or front == except_front:
            continue
        n = _job_reservation_count(record)
        if n is None:
            continue
        total += n
        if front not in seen:
            seen.add(front)
            names.append(front)
    return total, names


def _append_reservation_holding_the_lock(record: dict,
                                         session_id: str | None) -> dict:
    """Append one reservation line while the store's write lock is held."""
    entry = dict(record)
    if not entry.get("at"):
        entry["at"] = store.utcnow_iso()
    if session_id is not None and not entry.get("by"):
        entry["by"] = session_id
    if "build" not in entry:
        entry["build"] = store.current_build()
    ledger = Path(paths.reservations_path())
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def release_front(name: str, by: str | None) -> int:
    """Append ``released_at`` on every open reservation for ``name``.

    Returns how many lines were released. A front with none is a no-op.
    """
    when = store.utcnow_iso()
    released = 0
    for record in folded_reservations():
        if record.get("front") != name:
            continue
        if record.get("released_at") is not None:
            continue
        store.append_ledger(
            paths.reservations_path(),
            dict(record, released_at=when),
            session_id=by)
        released += 1
    return released


#: What the supervisor prompt's ``inputs`` field renders for a pre-v5 front.
OLD_SHAPE_INPUTS = (
    "(this front was started from an old-shape brief; see its tasks below)"
)

_NONE = "(none)"


def _shown(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return text or _NONE


def front_inputs_block(record: dict | None) -> str:
    """The folded front's v5 inputs as text, in prompt order.

    Goal, finish line, numbered decisions, one team line per entry, then
    one repository block. An old-shape front (or no record) is the
    parenthetical that points at the tasks block instead.
    """
    if record is None or record.get("shape") != "v5":
        return OLD_SHAPE_INPUTS
    lines = [
        "## Goal",
        "",
        _shown(record.get("goal")),
        "",
        "## Finish line",
        "",
        _shown(record.get("finish_line")),
        "",
        "## Decisions",
        "",
    ]
    decisions = record.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        lines.append(_NONE)
    else:
        for index, item in enumerate(decisions, 1):
            lines.append(f"{index}. {item}")
    lines += ["", "## Team", ""]
    team = record.get("team")
    wrote_team = False
    if isinstance(team, list):
        for entry in team:
            if not isinstance(entry, dict):
                continue
            lines.append(
                f"- role {entry.get('role')}, agent {entry.get('agent')}, "
                f"pool {entry.get('pool')}, model {entry.get('model')}, "
                f"effort {entry.get('effort')}, count {entry.get('count')}"
            )
            wrote_team = True
    if not wrote_team:
        lines.append(_NONE)
    lines += ["", "## Repositories", ""]
    repos = record.get("repositories")
    blocks: list[str] = []
    if isinstance(repos, list):
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            name = str(repo.get("name") or "").strip() or "(unnamed)"
            trailers = repo.get("trailers")
            if isinstance(trailers, list) and trailers:
                trailer_text = ", ".join(str(item) for item in trailers)
            else:
                trailer_text = _NONE
            pr_body = repo.get("pr_body")
            if not (isinstance(pr_body, str) and pr_body.strip()):
                pr_body = _NONE
            blocks.append("\n".join([
                f"### {name}",
                f"- url: {_shown(repo.get('url'))}",
                f"- base: {_shown(repo.get('base'))} at "
                f"{_shown(repo.get('base_sha'))}",
                f"- work: {_shown(repo.get('work'))}",
                f"- target: {_shown(repo.get('target'))}",
                "- policy:",
                f"  - check: {_shown(repo.get('check'))}",
                f"  - land: {_shown(repo.get('land'))}",
                f"  - trailers: {trailer_text}",
                f"  - pr-body: {pr_body}",
            ]))
    lines.append("\n\n".join(blocks) if blocks else _NONE)
    return "\n".join(lines)


def _existing_fronts() -> set[str]:
    """Names already on the ledger: a front directory holding a front line."""
    try:
        names = [entry.name for entry in paths.fronts_dir().iterdir()
                 if entry.is_dir()]
    except OSError:
        return set()
    return {name for name in names if read_front_record(name) is not None}


def _branch_violation(directory: str, branch: str) -> str | None:
    """None when refs/heads/<branch> exists in the repo holding directory.

    One `git show-ref` at most; repo membership is a directory walk, not a
    second subprocess. A directory outside any repository, or a missing git
    binary, is its own violation rather than a silent pass.
    """
    node = Path(directory) if Path(directory).is_dir() else Path(directory).parent
    if not any((parent / ".git").exists() for parent in (node, *node.parents)):
        return (f"field 'land-on' branch '{branch}' cannot be checked: "
                f"brief directory '{directory}' is not inside a git repository")
    try:
        completed = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet",
             f"refs/heads/{branch}"],
            cwd=str(directory),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return (f"field 'land-on' branch '{branch}' cannot be checked: "
                f"git is not available")
    if completed.returncode != 0:
        return f"field 'land-on' branch '{branch}' does not exist"
    return None


def _ls_remote(url: str, branch: str) -> tuple[str | None, str | None]:
    """The sha at ``refs/heads/<branch>`` on ``url``, or why it could not
    be read.

    Returns ``(sha, None)`` when the ref exists, ``(None, None)`` when
    git answered and the ref is absent, and ``(None, error)`` when the
    query itself failed.
    """
    try:
        completed = subprocess.run(
            ["git", "ls-remote", url, f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=30)
    except OSError:
        return None, "git is not available"
    except subprocess.TimeoutExpired:
        return None, "git ls-remote timed out"
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        return None, err or f"git ls-remote exited {completed.returncode}"
    line = completed.stdout.strip().splitlines()
    if not line:
        return None, None
    sha = line[0].split()[0] if line[0].split() else ""
    return (sha or None), None


def task_table_violations(task: dict, label: str, tag: str) -> list[str]:
    """Field-level violations for one task table, brief or commissioned.

    A commissioned task validates exactly as a brief's task does, so both
    readers share this: a title, a scope carrying all four headings, a
    verify command, a positive size, a well-shaped after list and
    well-typed extras. Cross-task rules (unique titles, known afters, no
    cycle) stay with the caller, which owns the whole list.
    """
    violations: list[str] = []
    title = task.get("title")
    if not (isinstance(title, str) and title):
        violations.append(f"{tag}: field 'title' is required")
    scope = task.get("scope")
    if not (isinstance(scope, str) and scope):
        violations.append(f"{label}: field 'scope' is required")
    else:
        missing = [part for part in _SCOPE_PARTS if part not in scope]
        if missing:
            violations.append(
                f"{label}: field 'scope' must contain WHAT, INPUTS, OUTPUTS "
                f"and OUT OF SCOPE (missing: {', '.join(missing)})")
    verify = task.get("verify")
    if not (isinstance(verify, str) and verify.strip()):
        violations.append(f"{label}: field 'verify' is required")
    size = task.get("size")
    if not _is_int(size) or (isinstance(size, int) and size < 1):
        violations.append(f"{label}: field 'size' must be a positive integer")
    task_after = task.get("after", [])
    if not isinstance(task_after, list):
        violations.append(f"{label}: field 'after' must be a list of task titles")
    else:
        for entry in task_after:
            if not (isinstance(entry, str) and entry):
                violations.append(f"{label}: field 'after' must be a list "
                                  f"of task titles")
    for key in ("timeout", "land-on"):
        if key in task and not isinstance(task[key], str):
            violations.append(f"{label}: field '{key}' must be a string")
    if "core" in task and not isinstance(task["core"], bool):
        violations.append(f"{label}: field 'core' must be true or false")
    return violations


def validate_commissioned_task(entry: dict, existing: list[dict]) -> list[str]:
    """Violations for one task commissioned onto a running front.

    The same contract as a brief's task: the field-level checks above,
    plus a title no task on the front already carries and an after list
    naming real tasks with no cycle. ``existing`` is the front's folded
    task ledger.
    """
    if not isinstance(entry, dict):
        return ["task must be a table"]
    title = entry.get("title")
    label = f"task '{title}'" if isinstance(title, str) and title else "task"
    violations = task_table_violations(entry, label, "task")
    titles = [task.get("title") for task in existing
              if isinstance(task, dict) and isinstance(task.get("title"), str)]
    known = set(titles)
    if isinstance(title, str) and title and title in known:
        violations.append(f"task title '{title}' is already on the front "
                          f"(task titles must be unique)")
    after = entry.get("after", [])
    if isinstance(after, list):
        for item in after:
            if isinstance(item, str) and item and item not in known:
                violations.append(f"{label}: field 'after' names unknown task "
                                  f"'{item}'")
    graph = {name: [dep for dep in _task_after(existing, name)
                    if isinstance(dep, str) and dep in known]
             for name in titles}
    if isinstance(title, str) and title and title not in known:
        graph[title] = [dep for dep in (after if isinstance(after, list) else [])
                        if isinstance(dep, str) and (dep in known or dep == title)]
    cycle = _find_cycle(graph)
    if cycle is not None:
        violations.append("task 'after' graph has a cycle: "
                          + " -> ".join(cycle))
    return violations


def _validate_v1_contract(data: dict, directory: str | None) -> list[str]:
    """want, done-when, land-on, allocation, tasks — the old brief."""
    violations: list[str] = []

    want = data.get("want")
    if not (isinstance(want, str) and want.strip()):
        violations.append("field 'want' is required (a non-empty string)")

    done_when = data.get("done-when")
    if done_when is None or (isinstance(done_when, str) and not done_when.strip()):
        violations.append("field 'done-when' is required (a non-empty string)")
    elif not isinstance(done_when, str) or not _is_one_sentence(done_when):
        violations.append("field 'done-when' must be one sentence")

    land_on = data.get("land-on")
    if not (isinstance(land_on, str) and land_on.strip()):
        violations.append("field 'land-on' is required (a non-empty string)")
    elif directory is not None:
        problem = _branch_violation(directory, land_on.strip())
        if problem is not None:
            violations.append(problem)

    allocation = data.get("allocation", {})
    if not isinstance(allocation, dict):
        violations.append("field 'allocation' must be a table of role to count")
        allocation = {}
    else:
        for role, count in allocation.items():
            if role not in JOB_ROLES:
                violations.append(f"field 'allocation' names unknown role '{role}'")
            elif not _is_int(count) or count < 0:
                violations.append(f"field 'allocation' for role '{role}' must be "
                                   f"a non-negative integer")

    tasks = data.get("task", [])
    if not isinstance(tasks, list):
        violations.append("field 'task' must be a list")
        tasks = []
    titles: list[str] = []
    for index, task in enumerate(tasks):
        tag = f"task #{index + 1}"
        if not isinstance(task, dict):
            violations.append(f"{tag} must be a table")
            continue
        title = task.get("title")
        label = f"task '{title}'" if isinstance(title, str) and title else tag
        violations.extend(task_table_violations(task, label, tag))
        if isinstance(title, str) and title:
            titles.append(title)
    seen: set[str] = set()
    for title in titles:
        if title in seen:
            violations.append(f"task title '{title}' is listed twice")
        seen.add(title)
    known = set(titles)
    for task in tasks:
        if not isinstance(task, dict):
            continue
        title = task.get("title")
        label = f"task '{title}'" if isinstance(title, str) and title else "task"
        task_after = task.get("after", [])
        if not isinstance(task_after, list):
            continue
        for entry in task_after:
            if isinstance(entry, str) and entry and entry not in known:
                violations.append(f"{label}: field 'after' names unknown task "
                                  f"'{entry}'")
    graph = {title: [entry for entry in _task_after(tasks, title)
                     if isinstance(entry, str) and entry in known]
             for title in titles}
    cycle = _find_cycle(graph)
    if cycle is not None:
        violations.append("task 'after' graph has a cycle: "
                          + " -> ".join(cycle))
    return violations


def _validate_identity(data: dict, existing: set[str]) -> list[str]:
    """The part of §4.3 a front that already ran must still satisfy.

    A closed front records history: its tasks are never written, its
    allocation never spends anything, and its brief cannot be corrected
    after the fact. What still has to hold is that it is a front, that it
    is named once, and that what it waits on exists.
    """
    return [line for line in _validate(data, existing)
            if line.startswith("field 'name'")
            or line.startswith("field 'after'")
            or line.startswith("front '")]


def _validate(data: dict, existing: set[str],
              directory: str | None = None, *,
              allow_existing_work: bool = False) -> list[str]:
    """Every 4.3 violation at once, each naming the field or rule it breaks."""
    violations: list[str] = []

    name = data.get("name")
    if name is None or (isinstance(name, str) and not name.strip()):
        violations.append("field 'name' is required (a non-empty string)")
        name = None
    elif not isinstance(name, str):
        violations.append("field 'name' must be a non-empty string")
        name = None
    elif not _NAME_RE.fullmatch(name.strip()):
        violations.append(f"field 'name' must be a directory-safe name "
                          f"(got '{name.strip()}')")
    elif name.strip() in existing:
        violations.append(f"front '{name.strip()}' is already on the ledger "
                          f"(field 'name' must be unique)")

    merge = data.get("merge")
    if merge is not None and merge not in ("self", "desk"):
        violations.append(f"field 'merge' must be 'self' or 'desk' or "
                          f"absent (got '{merge}')")

    for key in ("order", "prefer"):
        if key in data and not _is_int(data[key]):
            violations.append(f"field '{key}' must be an integer")

    after = data.get("after", [])
    if not isinstance(after, list):
        violations.append("field 'after' must be a list of front names")
        after = []
    else:
        for entry in after:
            if not (isinstance(entry, str) and entry):
                violations.append("field 'after' must be a list of front names")
            elif entry not in existing:
                violations.append(f"field 'after' names unknown front '{entry}'")

    if _is_v5_brief(data):
        violations.extend(
            _validate_v5(data, allow_existing_work=allow_existing_work))
    else:
        violations.extend(_validate_v1_contract(data, directory))

    monitors = data.get("monitor", [])
    if not isinstance(monitors, list):
        violations.append("field 'monitor' must be a list")
    else:
        for index, monitor in enumerate(monitors):
            if not isinstance(monitor, dict):
                violations.append(f"monitor #{index + 1} must be a table")
                continue
            measure = monitor.get("measure")
            if not (isinstance(measure, str) and measure.strip()):
                violations.append(f"monitor #{index + 1}: field 'measure' must be "
                                  f"a non-empty string")
            unit = monitor.get("unit")
            if not (isinstance(unit, str) and unit.strip()):
                violations.append(f"monitor #{index + 1}: field 'unit' must be "
                                  f"a non-empty string")
            every = monitor.get("every")
            if not (isinstance(every, str) and every.strip()):
                violations.append(f"monitor #{index + 1}: field 'every' must be "
                                  f"a non-empty string")
            alert = monitor.get("alert")
            if alert is not None and not (
                    isinstance(alert, str) and _ALERT_RE.match(alert)):
                violations.append(f"monitor #{index + 1}: field 'alert' does not "
                                  f"parse (got '{alert}')")
    return violations


def _task_after(tasks: list, title: str) -> list:
    for task in tasks:
        if isinstance(task, dict) and task.get("title") == title:
            after = task.get("after", [])
            return list(after) if isinstance(after, list) else []
    return []


def _job_role_for_pool(pool: str) -> str | None:
    """The JOB_ROLES name ``pool_for_role`` maps onto this pool.

    ``claude`` → ``opus``, ``codex`` → ``astra``; a pool named for its
    worker role answers with that role.
    """
    cfg = config.load()
    for role in JOB_ROLES:
        if cfg.pool_for_role(role) == pool:
            return role
    return None


def _agent_record(agent: str, effort: str) -> dict:
    manifest = pool_plugins.resolve_agent(agent)
    assert manifest is not None
    return {
        "agent": agent,
        "pool": manifest.name,
        "model": manifest.model,
        "effort": effort,
    }


def _team_records(entries: list) -> list[dict]:
    records: list[dict] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        parts = entry.strip().split(":")
        if len(parts) != 4:
            continue
        agent, effort, count, role = parts
        record = _agent_record(agent, effort)
        record["count"] = int(count)
        record["role"] = role
        records.append(record)
    return records


def _allocation_from_team(team: list[dict]) -> dict[str, int]:
    allocation: dict[str, int] = {}
    for entry in team:
        if entry.get("role") not in ("builder", "backup-builder", "reviewer"):
            continue
        role = _job_role_for_pool(str(entry.get("pool") or ""))
        if role is None:
            continue
        allocation[role] = allocation.get(role, 0) + int(entry["count"])
    return allocation


def _repo_record(repo: dict) -> dict:
    url = (repo.get("url") or "").strip()
    base = (repo.get("base") or "").strip()
    sha, _err = _ls_remote(url, base)
    land = repo.get("land")
    return {
        "name": (repo.get("name") or "").strip(),
        "url": url,
        "base": base,
        "work": (repo.get("work") or "").strip(),
        "target": (repo.get("target") or "").strip(),
        "check": (repo.get("check") or "").strip(),
        "land": land if land in _V5_LAND else "push",
        "trailers": list(repo["trailers"]) if isinstance(
            repo.get("trailers"), list) else [],
        "pr_body": repo["pr-body"] if isinstance(
            repo.get("pr-body"), str) else "",
        "base_sha": sha or "",
    }


def _monitors_from(data: dict) -> list[dict]:
    monitors = []
    for entry in data.get("monitor") or []:
        if not isinstance(entry, dict):
            continue
        monitors.append({key: entry[key] for key in
                         ("question", "measure", "unit", "of", "every", "alert")
                         if key in entry})
    return monitors


def _build_v5(data: dict, name: str,
              fixture: bool = False) -> tuple[dict, list[dict]]:
    """The front line a validated v5 brief becomes. No task lines."""
    goal = (data.get("goal") or "").strip()
    finish = (data.get("finish-line") or "").strip()
    decisions = [item.strip() for item in (data.get("decisions") or [])
                 if isinstance(item, str) and item.strip()]
    supervisor_raw = (data.get("supervisor") or "").strip()
    agent, effort = supervisor_raw.split(":", 1)
    supervisor = _agent_record(agent, effort)
    team = _team_records(data.get("team") or [])
    repos = [_repo_record(entry) for entry in (data.get("repository") or [])
             if isinstance(entry, dict)]
    front = entities.Front(
        id=ids.mint("front"),
        name=name,
        order=data.get("order", 0),
        prefer=data.get("prefer", 0),
        after=[entry for entry in (data.get("after") or [])],
        want=goal,
        done_when=finish,
        land_on=repos[0]["target"] if repos else "",
        reviews=data.get("reviews") or "",
        allocation=_allocation_from_team(team),
        supervisor=supervisor,
        brief_path=str(paths.brief_path(name)),
        state="queued",
        merge=data.get("merge") or "",
        fixture=fixture,
        shape="v5",
        goal=goal,
        finish_line=finish,
        decisions=decisions,
        team=team,
        repositories=repos,
    )
    front_line = front.to_dict()
    front_line["monitors"] = _monitors_from(data)
    entries = derive_team(front_line, [], [])
    if entries:
        front_line["working_team"] = [entry.to_dict() for entry in entries]
        front_line["derived_at"] = store.utcnow_iso()
    return front_line, []


def _build_adopt(data: dict, name: str, old: dict) -> dict:
    """The front line adopting an old-shape front to v5 shape.

    The brief supplies shape ``v5``, goal, finish line, decisions,
    team, repositories and the derived working team (built exactly as
    :func:`_build_v5` builds them). The running front keeps its id,
    supervisor, state, order, prefer, after and allocation; no task
    line is written and existing tasks, jobs, tree, map and milestones
    stay untouched. Brief-silent record fields (merge, reviews,
    monitors) stay as the front had them.
    """
    front_line, _ = _build_v5(
        data, name, fixture=bool(old.get("fixture")))
    front_line["id"] = old["id"]
    front_line["supervisor"] = old.get("supervisor")
    front_line["state"] = old.get("state") or "queued"
    front_line["order"] = old.get("order", 0)
    front_line["prefer"] = old.get("prefer", 0)
    front_line["after"] = list(old.get("after") or [])
    front_line["allocation"] = dict(old.get("allocation") or {})
    if not data.get("merge") and old.get("merge"):
        front_line["merge"] = old["merge"]
    if not data.get("reviews") and old.get("reviews"):
        front_line["reviews"] = old["reviews"]
    if not data.get("monitor") and isinstance(
            old.get("monitors"), list):
        front_line["monitors"] = old["monitors"]
    return front_line


def _build(data: dict, name: str,
           fixture: bool = False) -> tuple[dict, list[dict]]:
    """The front line and task lines a validated brief becomes."""
    if _is_v5_brief(data):
        return _build_v5(data, name, fixture=fixture)
    allocation = dict(data.get("allocation") or {})
    front = entities.Front(
        id=ids.mint("front"),
        name=name,
        order=data.get("order", 0),
        prefer=data.get("prefer", 0),
        after=[entry for entry in (data.get("after") or [])],
        want=(data.get("want") or "").strip(),
        done_when=(data.get("done-when") or "").strip(),
        land_on=data.get("land-on") or "",
        reviews=data.get("reviews") or "",
        allocation=allocation,
        supervisor=None,
        brief_path=str(paths.brief_path(name)),
        state="queued",
        merge=data.get("merge") or "",
        fixture=fixture,
    )
    front_line = front.to_dict()
    front_line["monitors"] = _monitors_from(data)
    task_lines = []
    for entry in data.get("task") or []:
        task_after = list(entry.get("after") or [])
        task = entities.Task(
            id=ids.mint("task"),
            front=name,
            title=entry.get("title") or "",
            scope=entry.get("scope") or "",
            verify=entry.get("verify") or "",
            size=entry.get("size") or 0,
            after=task_after,
            timeout=entry.get("timeout") or "",
            land_on=entry.get("land-on") or data.get("land-on") or "",
            core=bool(entry.get("core", False)),
            state="ready" if not task_after else "waiting",
            units_done=0,
            units_total=entry.get("size") or 0,
        )
        task_lines.append(task.to_dict())
    return front_line, task_lines


def _read_brief(directory: str, violations: list[str]) -> dict | None:
    src = Path(directory) / "brief.toml"
    try:
        raw = src.read_bytes()
    except FileNotFoundError:
        violations.append(f"brief '{src}' not found")
        return None
    except OSError as exc:
        violations.append(f"brief '{src}' is not readable ({exc})")
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except ValueError as exc:
        violations.append(f"brief '{src}' is not valid TOML ({exc})")
        return None
    if not isinstance(data, dict):
        violations.append(f"brief '{src}' must be a TOML table")
        return None
    return data


def front_add_main(directory: str, dry_run: bool = False,
                   fixture: bool = False, closed: bool = False,
                   adopt: bool = False) -> int:
    """Add a front from its brief.

    ``adopt`` upgrades a running old-shape front to v5 shape in place
    (see :func:`front_adopt_main`); it may not combine with ``fixture``
    or ``closed``.

    ``closed`` records a front that is already finished: the record goes on
    at state ``done`` and no task line is written. It exists because the
    queue's `after` names fronts by ledger record, and this swarm ran two
    fronts to completion before `front add` existed — so a brief could not
    wait on the front it really came after. Writing their tasks out as
    landed would be a claim the ledger has no evidence for; writing the
    front alone claims only what is true, that it ran and is done.
    """
    if adopt:
        if fixture or closed:
            extra = []
            if fixture:
                extra.append(
                    "field 'fixture' may not be combined with "
                    "field 'adopt'")
            if closed:
                extra.append(
                    "field 'closed' may not be combined with "
                    "field 'adopt'")
            return Refusal(extra).report()
        return front_adopt_main(directory, dry_run=dry_run)
    me, violations = caller.resolve("front add")
    caller.check_role(me, "front add", violations=violations)
    data = _read_brief(directory, violations)
    if data is not None:
        violations.extend(
            _validate_identity(data, _existing_fronts()) if closed
            else _validate(data, _existing_fronts(), directory))
    if violations:
        return Refusal(violations).report()
    assert data is not None
    name = data["name"].strip()
    front_line, task_lines = _build(data, name, fixture=fixture)
    if closed:
        front_line["state"] = "done"
        task_lines = []
    if dry_run:
        print(f"would write {paths.front_record_path(name)}:")
        print(json.dumps(front_line))
        print(f"would write {paths.front_tasks_path(name)}:")
        for line in task_lines:
            print(json.dumps(line))
        print(f"would copy {Path(directory) / 'brief.toml'} -> "
              f"{paths.brief_path(name)}")
        if (Path(directory) / "plan.md").is_file():
            print(f"would copy {Path(directory) / 'plan.md'} -> "
                  f"{paths.plan_path(name)}")
        return 0
    who = caller.by_line(me)
    dest_dir = paths.config_front_dir(name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(directory) / "brief.toml", paths.brief_path(name))
    if (Path(directory) / "plan.md").is_file():
        shutil.copyfile(Path(directory) / "plan.md", paths.plan_path(name))
    store.append_ledger(paths.front_record_path(name), front_line, session_id=who)
    for line in task_lines:
        store.append_ledger(paths.front_tasks_path(name), line, session_id=who)
    print(front_line["id"])
    return 0


def front_adopt_main(directory: str, dry_run: bool = False) -> int:
    """Upgrade a running old-shape front to v5 shape in place.

    Foreman-only, like ``front add``. Takes a v5-shape brief whose
    ``name`` is an existing front that is old-shape and not done, and
    appends one front line keeping the front's id, supervisor, state,
    order, prefer, after and allocation while adding shape ``v5``,
    goal, finish line, decisions, team, repositories and the derived
    working team. No task line is written. ``--dry-run`` prints the
    line that would be appended and writes nothing.

    The gate names this operation ``front adopt`` (not ``front add
    --adopt``) so the role-gate readers, which match gates to tools by
    CLI-path prefix, do not offer the plain ``front_add`` tool to the
    foreman: adoption stays a CLI operation on the ``front add``
    parser, refused to every role but the foreman (and the owner).
    """
    verb = "front adopt"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, caller.FOREMAN, violations=violations)
    data = _read_brief(directory, violations)
    name: str | None = None
    old: dict | None = None
    if data is not None:
        raw_name = data.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
            old = read_front_record(name)
            if old is None:
                violations.append(
                    f"unknown front '{name}' "
                    f"(field 'name' names no front on the ledger)")
            else:
                if old.get("shape") == "v5":
                    violations.append(
                        f"front '{name}' is already v5 shape "
                        f"(field 'shape' is already 'v5')")
                if old.get("state") == "done":
                    violations.append(
                        f"front '{name}' is done "
                        f"(field 'state' must not be 'done')")
        if not _is_v5_brief(data):
            violations.append(
                "brief is not v5 shape (field 'goal' is required)")
        else:
            existing = _existing_fronts() - ({name} if name else set())
            violations.extend(
                _validate(data, existing, directory,
                          allow_existing_work=True))
    if violations:
        return Refusal(violations).report()
    assert data is not None and name is not None and old is not None
    front_line = _build_adopt(data, name, old)
    if dry_run:
        print(f"would write {paths.front_record_path(name)}:")
        print(json.dumps(front_line))
        return 0
    who = caller.by_line(me)
    dest_dir = paths.config_front_dir(name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(directory) / "brief.toml", paths.brief_path(name))
    if (Path(directory) / "plan.md").is_file():
        shutil.copyfile(Path(directory) / "plan.md", paths.plan_path(name))
    store.append_ledger(paths.front_record_path(name), front_line,
                        session_id=who)
    print(front_line["id"])
    return 0


def _list_lines() -> list[str]:
    try:
        names = [entry.name for entry in paths.fronts_dir().iterdir()
                 if entry.is_dir()]
    except OSError:
        return []
    rows = []
    for name in names:
        record = read_front_record(name)
        try:
            tasks = store.fold_by_id(
                store.read_ledger(paths.front_tasks_path(name)))
        except OSError:
            tasks = []
        ready = sum(1 for task in tasks if task.get("state") == "ready")
        if record is None:
            rows.append((0, name,
                         f"{name} \u2014 no record \u00b7 tasks {ready}/{len(tasks)} "
                         f"ready \u00b7 waits for unknown"))
            continue
        prefer = record.get("prefer", 0)
        pending = [front for front in (record.get("after") or [])
                   if (read_front_record(front) or {}).get("state") != "done"]
        waits = ", ".join(pending) if pending else "nothing"
        mark = " \u00b7 fixture" if record.get("fixture") else ""
        rows.append((-prefer if isinstance(prefer, int) else 0, name,
                     f"{name}{mark} \u2014 {record.get('state')} "
                     f"\u00b7 prefer {prefer} "
                     f"\u00b7 tasks {ready}/{len(tasks)} ready "
                     f"\u00b7 waits for {waits}"))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in rows]


def front_list_main() -> int:
    me, violations = caller.resolve("front list")
    caller.check_role(me, "front list", violations=violations)
    caller.drop_role_refusal(me, "front list", violations)
    if violations:
        return Refusal(violations).report()
    for line in _list_lines():
        print(line)
    return 0


def front_queue_main() -> int:
    """Print queued fronts in order, each with why it waits."""
    me, violations = caller.resolve("front queue")
    caller.check_role(me, "front queue", caller.FOREMAN,
                      violations=violations)
    caller.drop_role_refusal(me, "front queue", violations)
    if violations:
        return Refusal(violations).report()
    queued = queued_fronts()
    for record in queued:
        name = record.get("name") or ""
        print(f"{name}  {queue_wait_reason(record, queued)}")
    return 0


def _revised(name: str, violations: list[str]) -> dict | None:
    if not (isinstance(name, str) and name.strip()):
        violations.append("field 'name' is required")
        return None
    record = read_front_record(name.strip())
    if record is None:
        violations.append(f"unknown front '{name.strip()}'")
        return None
    return record


def front_prefer_main(name: str, prefer: str | int) -> int:
    me, violations = caller.resolve("front prefer")
    caller.check_role(me, "front prefer", violations=violations)
    record = _revised(name, violations)
    try:
        number = int(str(prefer).strip())
    except (TypeError, ValueError, AttributeError):
        violations.append(f"field 'prefer' must be an integer (got '{prefer}')")
        number = None
    if violations:
        return Refusal(violations).report()
    assert record is not None and number is not None
    key = name.strip()
    updated = dataclasses.replace(
        entities.Front.from_dict(record), prefer=number).to_dict()
    if "monitors" in record:
        updated["monitors"] = record["monitors"]
    store.append_ledger(paths.front_record_path(key), updated,
                        session_id=caller.by_line(me))
    print(f"{key} prefer {number}")
    return 0


def set_front_supervisor(name: str, session_id: str | None, by: str) -> None:
    """Name the front's supervisor on its record, appending a revised copy.

    `launch supervisor` and `front take` both write it, the way `prefer`
    and `allocate` carry every other field over: the screen resolves the
    front's supervisor through this field first, so a summoned supervisor
    is never a roster line the screen cannot attribute. Nothing reads it
    as permission; the roster still says who may call.
    """
    key = (name or "").strip()
    if not key:
        return
    record = read_front_record(key)
    if record is None or record.get("supervisor") == session_id:
        return
    updated = dataclasses.replace(
        entities.Front.from_dict(record), supervisor=session_id).to_dict()
    if "monitors" in record:
        updated["monitors"] = record["monitors"]
    store.append_ledger(paths.front_record_path(key), updated,
                        session_id=by)


def _reload_collector() -> None:
    """Push a collector reload after a verb changed the world it observes.

    A new ceiling governs the next tick either way; the restart also clears
    a `collector stale` line with nothing closed by hand. Best effort: a
    ceiling change must never fail on its reload.
    """
    from . import collector as _collector

    try:
        _collector.reload_after_config_change()
    except Exception:  # noqa: BLE001 - the reload is never the verb's work
        pass


def front_allocate_main(name: str, role: str, count: str | int) -> int:
    """Set a front's ceiling for one role, appending a revised copy.

    The allocation used to change only by a hand-written front line, and
    the hand-written line carried the allocation and not ``state`` or
    ``prefer`` — folding is last-wins over whole records, so the front
    lost both. Like ``prefer`` and ``close`` this appends a revised copy
    of the whole record instead, built by ``dataclasses.replace`` over
    ``entities.Front.from_dict``, so every other field stays byte-identical.

    A ceiling below what the front currently holds is a refusal naming
    both numbers, not a silent over-subscription.
    """
    from . import capacity

    me, violations = caller.resolve("front allocate")
    caller.check_role(me, "front allocate", caller.FOREMAN,
                      violations=violations)
    record = _revised(name, violations)
    key_role = role.strip() if isinstance(role, str) else ""
    if not key_role:
        violations.append("field 'role' is required")
    elif config.load().pool_for_role(key_role) is None:
        violations.append(f"field 'role' names unknown role '{key_role}' "
                          f"(no pool serves it)")
    try:
        number = int(str(count).strip())
    except (TypeError, ValueError, AttributeError):
        violations.append(f"field 'count' must be an integer (got '{count}')")
        number = None
    else:
        if number < 0:
            violations.append(
                f"field 'count' must not be negative (got {number})")
            number = None
    if (record is not None and key_role and number is not None):
        held = capacity.held_by_front_role().get(
            (name.strip(), key_role), 0)
        if number < held:
            violations.append(
                f"role '{key_role}' on front '{name.strip()}': "
                f"{held} held, ceiling {number}")
    if violations:
        return Refusal(violations).report()
    assert record is not None and key_role and number is not None
    key = name.strip()
    allocation = dict(entities.Front.from_dict(record).allocation or {})
    allocation[key_role] = number
    updated = dataclasses.replace(
        entities.Front.from_dict(record),
        allocation=allocation).to_dict()
    if "monitors" in record:
        updated["monitors"] = record["monitors"]
    store.append_ledger(paths.front_record_path(key), updated,
                        session_id=caller.by_line(me))
    print(f"{key}: {key_role} ceiling {number}")
    _reload_collector()
    return 0


def _reserve_phases(phase: str) -> tuple[str, ...]:
    if phase == "all":
        return RESERVE_PHASES
    return (phase,)


def revise_front(name: str, record: dict, who: str | None, **changes) -> dict:
    """Append a revised copy of the front line with ``changes`` applied."""
    updated = dataclasses.replace(
        entities.Front.from_dict(record), **changes).to_dict()
    if "monitors" in record:
        updated["monitors"] = record["monitors"]
    store.append_ledger(paths.front_record_path(name), updated,
                        session_id=who)
    return updated


def reserve_front(name: str, record: dict, phase: str,
                  who: str | None) -> tuple[list[dict], list[str]]:
    """Hold this front's team for ``phase``.

    Returns (created lines, violations). Empty created and no
    violations means already held or nothing was wanted.
    """
    phases = _reserve_phases(phase)
    wanted = _wanted_reservations(record, phases)
    created: list[dict] = []
    violations: list[str] = []
    with store._write_lock():
        open_recs = open_reservations()
        open_keys = {(rec.get("front"), rec.get("pool"), rec.get("phase"))
                     for rec in open_recs}
        to_create = [item for item in wanted
                     if (name, item[0], item[3]) not in open_keys]
        new_by_pool: dict[str, int] = {}
        for pool, role, count, _phase in to_create:
            if role == "supervisor":
                continue
            new_by_pool[pool] = new_by_pool.get(pool, 0) + count
        for pool, count in new_by_pool.items():
            cap = _pool_cap(pool)
            if cap is None:
                continue
            reserved, others = _others_on_pool(open_recs, pool, name)
            if cap - reserved < count:
                by = f" by {', '.join(others)}" if others else ""
                violations.append(
                    f"pool {pool}: cap {cap}, reserved {reserved}{by}, "
                    f"wants {count}")
        if violations:
            return [], violations
        for pool, role, count, item_phase in to_create:
            created.append(_append_reservation_holding_the_lock(
                entities.Reservation(
                    id=ids.mint("reservation"),
                    front=name, pool=pool, role=role, count=count,
                    phase=item_phase, released_at=None,
                ).to_dict(),
                who))
    return created, []


def front_reserve_main(name: str, phase: str = "builders") -> int:
    """Hold this front's team against the pool cap until it is released.

    For each team entry of ``phase`` (builder and backup-builder are
    builders; reviewer is reviewers; supervisor nothing) append a
    reservation unless one is already open for that front, pool and
    phase. Refused, naming the numbers, when for any pool
    ``cap - (open reservations of other fronts) < count``. An old-shape
    front reserves its allocation as the builders phase.
    """
    me, violations = caller.resolve("front reserve")
    caller.check_role(me, "front reserve", caller.FOREMAN,
                      violations=violations)
    if phase not in RESERVE_PHASES and phase != "all":
        violations.append(
            f"field '--phase' must be builders, reviewers or all "
            f"(got '{phase}')")
    record = _revised(name, violations)
    if violations:
        return Refusal(violations).report()
    assert record is not None
    key = name.strip()
    who = caller.by_line(me)
    wanted = _wanted_reservations(record, _reserve_phases(phase))
    created, extra = reserve_front(key, record, phase, who)
    if extra:
        return Refusal(extra).report()
    if created:
        for rec in created:
            print(f"{key}: reserved {rec['pool']} {rec['count']} "
                  f"({rec['phase']})")
    elif wanted:
        print(f"{key}: reserved (already held)")
    else:
        print(f"{key}: reserved 0")
    _reload_collector()
    return 0


def front_release_main(name: str) -> int:
    """Give back every open reservation this front still holds."""
    me, violations = caller.resolve("front release")
    caller.check_role(me, "front release", caller.FOREMAN,
                      violations=violations)
    record = _revised(name, violations)
    if violations:
        return Refusal(violations).report()
    assert record is not None
    key = name.strip()
    n = release_front(key, caller.by_line(me))
    print(f"{key}: released {n}")
    _reload_collector()
    return 0


def _front_live_sessions(name: str) -> list[str]:
    """Roster ids of this front that are still starting, running or stalled."""
    sessions = caller.read_roster().get("sessions", {})
    found: list[str] = []
    for session_id, entry in sessions.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("front") != name:
            continue
        if entry.get("state") not in ("starting", "running", "stalled"):
            continue
        found.append(session_id)
    return found


def stop_front(name: str, reason: str, by: str | None) -> None:
    """Kill live sessions, release the team, mark ``name`` stopped.

    The verb and the collector's quota-ask ``stop X`` share this: the
    caller has already checked that the front is active and that a
    reason was given.
    """
    from . import launch as launch_module

    key = name.strip()
    for session_id in _front_live_sessions(key):
        launch_module.cmd_kill(
            argparse.Namespace(target=session_id, reason=reason))
    release_front(key, by)
    current = read_front_record(key)
    if current is None:
        return
    revise_front(key, current, by, state="stopped", stop_reason=reason)


def front_stop_main(name: str, reason: str | None) -> int:
    """Kill the front's live sessions, release its team, mark it stopped.

    Queued jobs stay queued. Refused unless the front is active, naming
    the state it is in.
    """
    me, violations = caller.resolve("front stop")
    caller.check_role(me, "front stop", caller.FOREMAN,
                      violations=violations)
    record = _revised(name, violations)
    text = (reason or "").strip() if isinstance(reason, str) else ""
    if not text:
        violations.append("field '--reason' is required for 'front stop'")
    if record is not None and record.get("state") != "active":
        shown = record.get("state") or "unknown"
        violations.append(
            f"front '{name.strip()}' is {shown}, not active")
    if violations:
        return Refusal(violations).report()
    assert record is not None
    key = name.strip()
    stop_front(key, text, caller.by_line(me))
    print(f"{key} stopped")
    return 0


def front_resume_main(name: str) -> int:
    """Re-queue a stopped front with ``prefer`` unchanged.

    It takes its place in the order and starts through the tick like
    any other queued front. Refused unless the front is stopped.
    """
    me, violations = caller.resolve("front resume")
    caller.check_role(me, "front resume", caller.FOREMAN,
                      violations=violations)
    record = _revised(name, violations)
    if record is not None and record.get("state") != "stopped":
        shown = record.get("state") or "unknown"
        violations.append(
            f"front '{name.strip()}' is {shown}, not stopped")
    if violations:
        return Refusal(violations).report()
    assert record is not None
    key = name.strip()
    revise_front(key, record, caller.by_line(me),
                 state="queued", stop_reason="")
    print(f"{key} queued")
    return 0


def front_close_main(name: str, merged: str | None = None) -> int:
    """Mark a front done, landing its built tasks where told.

    `--merged <sha>` is the owner's word that the front's branch is on its
    target: every task still at `built` lands at that sha through the same
    gate a landing always passes, so a merged front's ledger says what its
    target says instead of reading "built, never landed" forever.
    """
    me, violations = caller.resolve("front close")
    caller.check_role(me, "front close", violations=violations)
    record = _revised(name, violations)
    sha = (merged or "").strip() if merged is not None else None
    if merged is not None and not sha:
        violations.append("field '--merged' must name the merged commit "
                          f"(got '{merged}')")
    if violations:
        return Refusal(violations).report()
    assert record is not None
    key = name.strip()
    if sha is not None:
        from .progress import task_landed_main

        try:
            tasks = store.fold_by_id(
                store.read_ledger(paths.front_tasks_path(key)))
        except OSError:
            tasks = []
        for task in tasks:
            if task.get("state") != "built":
                continue
            if task_landed_main(str(task.get("id")), head=sha) != 0:
                return 1
    updated = dataclasses.replace(
        entities.Front.from_dict(record), state="done").to_dict()
    if "monitors" in record:
        updated["monitors"] = record["monitors"]
    store.append_ledger(paths.front_record_path(key), updated,
                        session_id=caller.by_line(me))
    release_front(key, caller.by_line(me))
    print(f"{key} closed")
    return 0


def _finish_blocked_reason(name: str, record: dict) -> str | None:
    """Why a v5 front cannot finish, or None when it is not behind.

    Refused while ``behind`` is set or a rebase or front-landing item
    is open or failed, naming the target and the sha.
    """
    from . import node as node_mod
    from . import progress as progress_mod

    folded, _by_id = node_mod._read_nodes(name)
    behind = str(record.get("behind") or "").strip()
    blocking = progress_mod._open_script_item(
        folded, "rebase",
        states=progress_mod._BLOCKING_SCRIPT_STATES)
    if blocking is None:
        blocking = progress_mod._open_script_item(
            folded, "front-landing",
            states=progress_mod._BLOCKING_SCRIPT_STATES)
    if not behind and blocking is None:
        return None
    target = ""
    repos = record.get("repositories") or []
    if isinstance(repos, list):
        for entry in repos:
            if not isinstance(entry, dict):
                continue
            target = str(entry.get("target") or "").strip()
            if target:
                break
    sha = behind
    if not sha and blocking is not None:
        sha = str(blocking.get("onto") or blocking.get("base_sha")
                  or "").strip()
    if not sha:
        sha = str(record.get("base_sha") or "").strip()
        if not sha and isinstance(repos, list):
            for entry in repos:
                if isinstance(entry, dict):
                    sha = str(entry.get("base_sha") or "").strip()
                    if sha:
                        break
    msg = f"front {name} is behind {target} ({sha})"
    if blocking is not None:
        kind = str(blocking.get("kind") or "script")
        state = str(blocking.get("state") or "queued")
        msg += f"; its {kind} item {blocking.get('id')} is {state}"
    return msg


def front_done_main(name: str) -> int:
    """Mark a front done: every task landed, every monitor measured.

    The supervisor's verb (DESIGN §3 step 8, §12): the owner's
    `front close` stays for closing early. Refused — naming which tasks
    are not landed and which monitors have no measurement — unless the
    front is truly finished, and then it appends a revised copy of the
    front line at state `done`, the way `front close` does. A v5 front
    that is behind its target, or that still has a rebase or
    front-landing item open or failed, is refused naming the target
    and the sha.
    """
    verb = "front done"
    me, violations = caller.resolve(verb)
    record = _revised(name, violations)
    key = (name or "").strip()
    caller.check_front_supervisor(me, key or None, verb,
                                  violations=violations)
    tasks: list[dict] = []
    if record is not None:
        try:
            tasks = store.fold_by_id(
                store.read_ledger(paths.front_tasks_path(key)))
        except OSError:
            tasks = []
        waiting = [(task.get("title") or "(untitled)",
                    task.get("state") or "unknown")
                   for task in tasks
                   if task.get("state") != "landed"]
        if waiting:
            violations.append(
                f"front '{key}' is not done: tasks not landed: "
                + ", ".join(f"'{title}' ({state})"
                            for title, state in waiting))
        declared = record.get("monitors")
        declared = declared if isinstance(declared, list) else []
        try:
            measured = store.read_ledger(paths.front_measurements_path(key))
        except OSError:
            measured = []
        unmeasured = []
        for decl in declared:
            if not isinstance(decl, dict):
                continue
            if not monitors.matching_measurements(measured, decl):
                label = (str(decl.get("question") or "").strip()
                         or str(decl.get("measure") or "").strip()
                         or "(unnamed monitor)")
                unmeasured.append(label)
        if unmeasured:
            violations.append(
                f"front '{key}' is not done: monitors with no measurement: "
                + ", ".join(f"'{label}'" for label in unmeasured))
        blocked = _finish_blocked_reason(key, record)
        if blocked:
            violations.append(blocked)
        from . import progress as progress_mod

        stale = progress_mod.stale_landed_reason(key)
        if stale:
            violations.append(stale)
    if violations:
        return Refusal(violations).report()
    assert record is not None
    updated = dataclasses.replace(
        entities.Front.from_dict(record), state="done").to_dict()
    if "monitors" in record:
        updated["monitors"] = record["monitors"]
    store.append_ledger(paths.front_record_path(key), updated,
                        session_id=caller.by_line(me))
    release_front(key, caller.by_line(me))
    print(f"{key} done")
    return 0


def front_take_main(name: str) -> int:
    """Move the calling supervisor's roster entry to another front.

    A supervisor that carries a front to done and is then given the next
    one had no way to say so: the roster is written by the launcher and by
    `register`, and neither moves a session. The only way through was to
    mint a second identity for the same process and leave a silent
    supervisor behind on a closed front — one process claiming to be two
    sessions, which is the lie the roster exists to prevent.

    The front it leaves must be done, so this can never quietly abandon a
    live front, and the front it takes must have no live supervisor of its
    own, which is the same limit `launch supervisor` enforces.
    """
    from . import launch

    verb = "front take"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, caller.SUPERVISOR, violations=violations)
    key = (name or "").strip()
    record = None
    if not key:
        violations.append("field 'front' is required")
    else:
        record = read_front_record(key)
        if record is None:
            violations.append(f"unknown front '{key}'")
    mine = str((me.session if me is not None else {}).get("front") or "")
    if me is not None and mine:
        leaving = read_front_record(mine)
        if mine == key:
            violations.append(f"session '{me.session_id}' already "
                              f"supervises '{key}'")
        elif leaving is not None and leaving.get("state") != "done":
            violations.append(
                f"session '{me.session_id}' supervises '{mine}', which is "
                f"'{leaving.get('state')}': close it with `foreman front "
                f"close {mine}` before taking another")
    if record is not None and me is not None:
        held = launch.live_supervisors(key, ignore=me.session_id)
        if held:
            other = held[0][0]
            violations.append(
                f"front '{key}' already has a live supervisor '{other}'; "
                f"one front has one supervisor")
    if violations:
        return Refusal(violations).report()
    assert me is not None and me.session_id
    sid = me.session_id

    def move(roster):
        sessions = (roster or {}).get("sessions")
        if isinstance(sessions, dict) and sid in sessions:
            sessions[sid] = dict(sessions[sid], front=key)
        return roster

    store.update_snapshot(paths.roster_path(), move,
                          default={"sessions": {}})
    set_front_supervisor(key, sid, by=sid)
    if mine and mine != key:
        set_front_supervisor(mine, None, by=sid)
    print(f"{sid} supervises {key}")
    return 0


def front_show_text(name: str, record: dict) -> str:
    """The folded front as text: inputs, then state, session, allocation.

    Old-shape fronts then list each task with its state and units. The
    inputs block is the same string the supervisor prompt interpolates.
    """
    supervisor = record.get("supervisor")
    if isinstance(supervisor, str) and supervisor.strip():
        session = supervisor.strip()
    else:
        session = _NONE
    allocation = record.get("allocation")
    if isinstance(allocation, dict) and allocation:
        alloc = ", ".join(
            f"{role} {count}"
            for role, count in sorted(allocation.items()))
    else:
        alloc = _NONE
    chunks = [
        front_inputs_block(record),
        f"state: {_shown(record.get('state'))}\n"
        f"supervisor session: {session}\n"
        f"allocation: {alloc}",
    ]
    if record.get("shape") == "v5":
        return "\n\n".join(chunks)
    try:
        tasks = store.fold_by_id(
            store.read_ledger(paths.front_tasks_path(name)))
    except OSError:
        tasks = []
    if tasks:
        chunks.append("\n".join(
            f'- "{task.get("title") or "(untitled)"}" — '
            f'{task.get("state") or "unknown"} · units '
            f'{task.get("units_done", 0)}/{task.get("units_total", 0)}'
            for task in tasks
        ))
    return "\n\n".join(chunks)


def front_show_main(name: str, as_json: bool = False) -> int:
    """Print one front's folded record as text, or as JSON.

    The owner at a terminal, the foreman, and the front's own supervisor
    may read it. A supervisor of another front is refused by name.
    """
    verb = "front show"
    me, violations = caller.resolve(verb)
    key = (name or "").strip()
    if not key:
        violations.append("field 'name' is required")
    record = read_front_record(key) if key else None
    if key and record is None:
        violations.append(f"unknown front '{key}'")
    caller.check_role(me, verb, caller.FOREMAN, caller.SUPERVISOR,
                      violations=violations)
    caller.check_visible_front(me, key or None, violations=violations)
    if violations:
        return Refusal(violations).report()
    assert record is not None
    if as_json:
        print(json.dumps(record))
        return 0
    print(front_show_text(key, record))
    return 0


def front_team_main(name: str) -> int:
    """Print the derived working team, one line per role, and record it.

    The owner, the foreman, and the front's own supervisor may read it.
    Derives from the current tree and map so a queued front that has
    grown a tree shows the live count, then writes ``working_team``.
    """
    from . import team as team_mod

    verb = "front team"
    me, violations = caller.resolve(verb)
    key = (name or "").strip()
    if not key:
        violations.append("field 'name' is required")
    record = read_front_record(key) if key else None
    if key and record is None:
        violations.append(f"unknown front '{key}'")
    if me is not None and me.role == caller.SUPERVISOR:
        caller.check_front_supervisor(me, key or None, verb,
                                      violations=violations)
    else:
        caller.check_role(me, verb, caller.FOREMAN, violations=violations)
    if violations:
        return Refusal(violations).report()
    assert record is not None
    tree = team_mod.load_tree(key)
    facts = team_mod.load_facts(key)
    entries = derive_team(record, tree, facts)
    updated = record_working_team(
        key, record, tree=tree, facts=facts, who=caller.by_line(me))
    if updated is not None:
        record = updated
    for entry in entries:
        print(format_team_line(entry))
    return 0


def front_import_main(front: str, directory: str | None,
                      seen_at: str | None = None,
                      commit: str | None = None,
                      dry_run: bool = False) -> int:
    """Read map.md, milestones.jsonl and tree.jsonl through the doors."""
    verb = "front import"
    me, violations = caller.resolve(verb)
    caller.check_front_supervisor(me, (front or "").strip() or None, verb,
                                  violations=violations)
    from . import front_import as impl
    return impl.front_import_main(
        front, directory, seen_at=seen_at, commit=commit, dry_run=dry_run)


def _pick_repository(record: dict, repo: str | None) -> tuple[dict | None, str]:
    """The matching ``[[repository]]`` entry, or a refusal reason."""
    repos = record.get("repositories") or []
    repos = [entry for entry in repos if isinstance(entry, dict)] if (
        isinstance(repos, list)) else []
    if not repos:
        return None, "front names no repository"
    want = (repo or "").strip()
    if not want:
        return repos[0], ""
    for entry in repos:
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if name == want or url == want:
            return entry, ""
    listed = ", ".join(
        str(entry.get("name") or "").strip() or "(unnamed)"
        for entry in repos) or "(none)"
    return None, (
        f"unknown repository '{want}' on front "
        f"'{record.get('name') or ''}' (known: {listed})")


def front_land_main(name: str, repo: str | None = None) -> int:
    """Queue a front-landing script item under the last milestone.

    Foreman or owner. ``lands`` is the work branch, ``target`` is the
    target branch. Refused while a front-landing item for that work
    branch is already open.
    """
    verb = "front land"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, caller.FOREMAN, violations=violations)
    record = _revised(name, violations)
    entry: dict | None = None
    if record is not None:
        if str(record.get("shape") or "") != "v5":
            violations.append(
                f"front '{name.strip()}' is not v5; only a v5 front is landed")
        else:
            entry, why = _pick_repository(record, repo)
            if why:
                violations.append(why)
            elif entry is not None:
                work = str(entry.get("work") or "").strip()
                target = str(entry.get("target") or "").strip()
                if not work:
                    violations.append("repository names no work branch")
                if not target:
                    violations.append("repository names no target branch")
                if work:
                    from . import node as node_mod
                    from . import progress as progress_mod

                    folded, _by_id = node_mod._read_nodes(name.strip())
                    open_item = progress_mod._open_script_item(
                        folded, "front-landing", lands=work)
                    if open_item is not None:
                        violations.append(
                            f"landing item for {work} is already open "
                            f"({open_item.get('id')})")
    if violations:
        return Refusal(violations).report()
    assert record is not None and entry is not None
    from . import progress as progress_mod

    who = caller.by_line(me)
    key = name.strip()
    work = str(entry.get("work") or "").strip()
    target = str(entry.get("target") or "").strip()
    repo_name = str(entry.get("name") or "").strip()
    lid = progress_mod.queue_script_item(
        key, record, kind="front-landing",
        title=f"land {key} onto {target}",
        repo=repo_name, lands=work, who=who, target=target)
    print(lid)
    return 0


def front_policy_main(front: str, repo: str) -> int:
    """Print the landing policy for a front and a repository, one field per line."""
    verb = "front policy"
    me, violations = caller.resolve(verb)
    key = (front or "").strip()
    want = (repo or "").strip()
    if not key:
        violations.append("field 'front' is required")
    if not want:
        violations.append("field 'repo' is required")
    record = read_front_record(key) if key else None
    if key and record is None:
        violations.append(f"unknown front '{key}'")
    caller.check_role(me, verb, caller.FOREMAN, caller.SUPERVISOR,
                      violations=violations)
    caller.check_visible_front(me, key or None, violations=violations)
    if violations:
        return Refusal(violations).report()
    from . import landing
    try:
        policy = landing.policy_for(key, want)
    except Refusal as exc:
        return exc.report()
    print(landing.format_policy(policy))
    return 0


def add_front_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="front_verb", required=True)
    add = verbs.add_parser("add", help="Validate a brief and append its front.")
    add.add_argument("directory", help="directory holding brief.toml (and plan.md)")
    add.add_argument("--dry-run", action="store_true",
                     help="validate and print what would be written; write nothing")
    add.add_argument("--fixture", action="store_true",
                     help="a front for trying the runtime out, marked as such "
                          "wherever it appears")
    add.add_argument("--closed", action="store_true",
                     help="a front that already ran and is done: the record "
                          "only, no tasks")
    add.add_argument("--adopt", action="store_true",
                     help="upgrade a running old-shape front to v5 shape in "
                          "place from a v5 brief naming it")
    verbs.add_parser("list", help="Print one line per front.")
    verbs.add_parser(
        "queue",
        help="Print queued fronts in order, each with why it waits.")
    show = verbs.add_parser("show", help="Print a front's folded record.")
    show.add_argument("name", help="front name")
    show.add_argument("--json", dest="as_json", action="store_true",
                      help="print the folded front line as JSON")
    team = verbs.add_parser(
        "team", help="Print the derived working team with reasons.")
    team.add_argument("name", help="front name")
    prefer = verbs.add_parser("prefer", help="Set a front's queue preference.")
    prefer.add_argument("name", help="front name")
    prefer.add_argument("prefer", help="new preference (integer)")
    allocate = verbs.add_parser(
        "allocate", help="Set a front's ceiling for one role.")
    allocate.add_argument("name", help="front name")
    allocate.add_argument("role", help="worker role")
    allocate.add_argument("count", help="new ceiling (non-negative integer)")
    reserve = verbs.add_parser(
        "reserve", help="Hold a front's team against the pool cap.")
    reserve.add_argument("name", help="front name")
    reserve.add_argument(
        "--phase", default="builders",
        choices=("builders", "reviewers", "all"),
        help="builders, reviewers or all (default builders)")
    release = verbs.add_parser(
        "release", help="Give back a front's open reservations.")
    release.add_argument("name", help="front name")
    stop = verbs.add_parser(
        "stop", help="Stop a running front and give its team back.")
    stop.add_argument("name", help="front name")
    stop.add_argument("--reason", default=None,
                      help="why it is being stopped; it lands on the record")
    resume = verbs.add_parser(
        "resume", help="Re-queue a stopped front.")
    resume.add_argument("name", help="front name")
    close = verbs.add_parser("close", help="Mark a front done.")
    close.add_argument("name", help="front name")
    close.add_argument("--merged", default=None,
                       help="the front's branch is on its target at this "
                            "commit: land every built task there")
    take = verbs.add_parser(
        "take", help="Move the calling supervisor to this front.")
    take.add_argument("name", help="front name")
    done = verbs.add_parser(
        "done", help="Mark a front done once every task landed and every "
                     "monitor measured (the supervisor's verb).")
    done.add_argument("name", help="front name")
    imp = verbs.add_parser(
        "import", help="Read map.md, milestones.jsonl and tree.jsonl "
                       "through the doors.")
    imp.add_argument("front", help="front to import into")
    imp.add_argument("directory",
                     help="directory holding map.md, milestones.jsonl "
                          "and tree.jsonl")
    imp.add_argument("--seen-at", dest="seen_at", default=None,
                     help="timestamp every imported map fact was observed "
                          "(required when map.md exists)")
    imp.add_argument("--commit", default=None,
                     help="commit every imported map fact was read at "
                          "(required when map.md exists)")
    imp.add_argument("--dry-run", action="store_true",
                     help="print the counts and write nothing")
    policy = verbs.add_parser(
        "policy", help="Print the landing policy for a repository.")
    policy.add_argument("front", help="front name")
    policy.add_argument("repo", help="repository name or url")
    land = verbs.add_parser(
        "land", help="Queue a script that lands the front onto its target.")
    land.add_argument("name", help="front name")
    land.add_argument("--repo", default=None,
                      help="repository name (default: the first)")


@cli.subcommand("front", help="Add, list, show, team, queue, policy, land, prefer, allocate, "
                     "reserve, release, stop, resume, close, take, import or mark a "
                     "front done.")
def _front_entry(args: argparse.Namespace) -> int:
    if args.front_verb == "add":
        return front_add_main(args.directory, dry_run=args.dry_run,
                              fixture=args.fixture, closed=args.closed,
                              adopt=args.adopt)
    if args.front_verb == "list":
        return front_list_main()
    if args.front_verb == "queue":
        return front_queue_main()
    if args.front_verb == "show":
        return front_show_main(args.name, as_json=args.as_json)
    if args.front_verb == "team":
        return front_team_main(args.name)
    if args.front_verb == "prefer":
        return front_prefer_main(args.name, args.prefer)
    if args.front_verb == "allocate":
        return front_allocate_main(args.name, args.role, args.count)
    if args.front_verb == "reserve":
        return front_reserve_main(args.name, phase=args.phase)
    if args.front_verb == "release":
        return front_release_main(args.name)
    if args.front_verb == "stop":
        return front_stop_main(args.name, reason=args.reason)
    if args.front_verb == "resume":
        return front_resume_main(args.name)
    if args.front_verb == "close":
        return front_close_main(args.name, merged=args.merged)
    if args.front_verb == "take":
        return front_take_main(args.name)
    if args.front_verb == "done":
        return front_done_main(args.name)
    if args.front_verb == "import":
        return front_import_main(
            args.front, args.directory, seen_at=args.seen_at,
            commit=args.commit, dry_run=args.dry_run)
    if args.front_verb == "policy":
        return front_policy_main(args.front, args.repo)
    if args.front_verb == "land":
        return front_land_main(args.name, repo=args.repo)
    raise AssertionError(f"unknown front verb {args.front_verb!r}")


_front_entry.add_arguments = add_front_arguments  # type: ignore[attr-defined]
