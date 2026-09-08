"""Entities from docs/DESIGN.md section 5.

Frozen dataclasses with symmetric to_dict / from_dict. from_dict ignores
unknown keys so an older ledger line still loads.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, ClassVar

FRONT_STATES = ("queued", "active", "done", "halted", "frozen")
TASK_STATES = ("waiting", "ready", "active", "built", "landed")
JOB_STATES = ("planned", "queued", "running", "returned", "verified", "failed", "killed")
JOB_KINDS = ("implement", "review", "merge", "research", "verify")
JOB_ROLES = ("opus", "muse", "astra", "grok")
MERGE_STATES = ("requested", "merging", "landed", "failed")
SESSION_STATES = ("running", "exited", "stalled", "killed")
RULING_SCOPES = ("swarm", "front")
RULING_SOURCES = ("owner", "foreman", "supervisor")
EVIDENCE_STATUSES = ("CONFIRMED", "PLAUSIBLE")
INBOX_KINDS = ("money", "irreversible", "scope", "error")


class Entity:
    _aliases: ClassVar[dict[str, str]] = {}

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        for attr, key in self._aliases.items():
            if attr in data:
                data[key] = data.pop(attr)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        reverse = {key: attr for attr, key in cls._aliases.items()}
        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {}
        for key, value in data.items():
            name = reverse.get(key, key)
            if name in known:
                kwargs[name] = value
        return cls(**kwargs)


@dataclass(frozen=True)
class Front(Entity):
    id: str | None = None
    name: str = ""
    order: int = 0
    prefer: int = 0
    after: list[str] = field(default_factory=list)
    want: str = ""
    done_when: str = ""
    land_on: str = ""
    reviews: str = ""
    allocation: dict[str, int] = field(default_factory=dict)
    supervisor: str | None = None
    brief_path: str = ""
    state: str = "queued"


@dataclass(frozen=True)
class Task(Entity):
    id: str | None = None
    front: str | None = None
    title: str = ""
    scope: str = ""
    verify: str = ""
    size: int = 0
    after: list[str] = field(default_factory=list)
    timeout: str = ""
    land_on: str = ""
    state: str = "waiting"
    units_done: int = 0
    units_total: int = 0


@dataclass(frozen=True)
class Job(Entity):
    id: str | None = None
    task: str | None = None
    kind: str = ""
    role: str = ""
    priority: int = 0
    spec_path: str = ""
    session: str | None = None
    worktree: str = ""
    branch: str = ""
    log: str = ""
    timeout: str = ""
    units: list[int] = field(default_factory=list)
    attempt: int = 1
    state: str = "planned"
    planned_at: str | None = None
    queued_at: str | None = None
    started_at: str | None = None
    returned_at: str | None = None
    verified_at: str | None = None
    artifact: str = ""
    verdict_path: str = ""


@dataclass(frozen=True)
class Merge(Entity):
    id: str | None = None
    branch: str = ""
    tasks: list[str] = field(default_factory=list)
    target: str = ""
    review_refs: list[str] = field(default_factory=list)
    head: str = ""
    result: str = ""
    requested_at: str | None = None
    landed_at: str | None = None
    by: str | None = None


@dataclass(frozen=True)
class Session(Entity):
    id: str | None = None
    role: str = ""
    pool: str = ""
    model: str = ""
    front: str | None = None
    job: str | None = None
    pid: int | None = None
    pgid: int | None = None
    pid_starttime: int | None = None
    worktree: str = ""
    log: str = ""
    timeout: str = ""
    launched_by: str | None = None
    started_at: str | None = None
    last_declared_at: str | None = None
    last_observed_at: str | None = None
    cpu_s: float = 0.0
    state: str = "running"


@dataclass(frozen=True)
class Pool(Entity):
    name: str = ""
    model: str = ""
    adapter: str = ""
    slots_total: int = 0
    kinds: list[str] = field(default_factory=list)
    cannot_take: list[str] = field(default_factory=list)
    timeout_default: str = ""
    meter: float | None = None
    skill: str = ""


@dataclass(frozen=True)
class Allocation(Entity):
    front: str = ""
    role: str = ""
    count: int = 0


@dataclass(frozen=True)
class SlotGrant(Entity):
    pool: str = ""
    front: str = ""
    role: str = ""
    job: str | None = None
    session: str | None = None
    granted_at: str | None = None
    released_at: str | None = None


@dataclass(frozen=True)
class Ruling(Entity):
    id: str | None = None
    scope: str = ""
    text: str = ""
    source: str = ""
    acks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Evidence(Entity):
    on: str = ""
    claim: str = ""
    status: str = ""
    command: str = ""
    output_ref: str = ""


@dataclass(frozen=True)
class Finding(Entity):
    _aliases: ClassVar[dict[str, str]] = {"class_": "class"}

    on: str = ""
    class_: str = ""
    title: str = ""
    detail: str = ""
    evidence_ref: str = ""


@dataclass(frozen=True)
class Measurement(Entity):
    monitor: str = ""
    value: float = 0.0
    of: float | None = None
    status: str = ""
    command: str = ""
    output_ref: str = ""


@dataclass(frozen=True)
class Checkpoint(Entity):
    session: str | None = None
    doing: str = ""
    next: str = ""
    held: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    jobs_running: list[str] = field(default_factory=list)
    last_event: str | None = None


@dataclass(frozen=True)
class InboxItem(Entity):
    _aliases: ClassVar[dict[str, str]] = {"from_": "from"}

    id: str | None = None
    from_: str = ""
    kind: str = ""
    question: str = ""
    recommendation: str = ""
    options: list[str] = field(default_factory=list)
    asked_at: str | None = None
    answered_at: str | None = None
    answer: str | None = None
    answered_by: str | None = None
    relayed: bool = False


@dataclass(frozen=True)
class Anomaly(Entity):
    kind: str = ""
    subject: str = ""
    since: str | None = None
    detail: str = ""
    resolved_at: str | None = None


@dataclass(frozen=True)
class Event(Entity):
    at: str | None = None
    kind: str = ""
    subject: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Digest(Entity):
    at: str | None = None
    text: str = ""
    computed: dict[str, Any] = field(default_factory=dict)
