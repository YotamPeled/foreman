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
JOB_STATES = ("planned", "queued", "running", "returned", "returned-with-work",
              "verified", "failed", "killed", "history")
JOB_KINDS = ("implement", "review", "merge", "research", "verify")
JOB_ROLES = ("opus", "muse", "astra", "grok")
#: Roles a roster session may carry: every worker role, plus the two
#: interactive ones the launcher and `register` mint.
SESSION_ROLES = JOB_ROLES + ("supervisor", "foreman", "merge-desk")
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
    #: How this front's built tasks land: "" (the merge desk lands them
    #: from a merge request) or "self" (the front's own supervisor calls
    #: `task landed` itself — Foreman's own fronts, by owner ruling).
    merge: str = ""
    #: A front that exists to try the runtime out, not to ship anything.
    #: It is marked wherever the front appears, so nobody reads a task a
    #: probe job moved as work the front actually did.
    fixture: bool = False


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
    #: The brief's `core = true`: only a core task may take an Opus worker.
    core: bool = False
    state: str = "waiting"
    units_done: int = 0
    units_total: int = 0
    #: Who commissioned this task onto a running front: "" for a brief's
    #: own tasks, otherwise the role that added it ("supervisor", "owner").
    #: The screen marks such a task "added by <role>".
    added_by: str = ""


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
    #: How many units the job was launched to do. Records written before
    #: the count change carry a list of unit ids; :func:`foreman.progress.
    #: job_units_count` folds both shapes, so the old lists keep crediting
    #: their length.
    units: int = 0
    #: The base ref the job's branch was cut from. Records written before
    #: this field existed carry none, so a branch-moved check on them
    #: answers False and a failure stays a failure.
    base: str = ""
    #: The `--because` sentence a failed or killed job was verified with.
    #: Empty on every other job; the screen renders it beside the job.
    verify_because: str = ""
    attempt: int = 1
    state: str = "planned"
    planned_at: str | None = None
    queued_at: str | None = None
    started_at: str | None = None
    returned_at: str | None = None
    verified_at: str | None = None
    artifact: str = ""
    verdict_path: str = ""
    #: The worker's exit code as the collector read it from a finish
    #: marker, or None when no marker was read. A waiter prints this.
    exit_code: int | None = None
    #: Why the collector moved the job, when the cause differs from the
    #: state. A timeout kill is failed with outcome_reason "job timed out".
    outcome_reason: str = ""


@dataclass(frozen=True)
class Merge(Entity):
    id: str | None = None
    front: str = ""
    branch: str = ""
    tasks: list[str] = field(default_factory=list)
    target: str = ""
    review_refs: list[str] = field(default_factory=list)
    head: str = ""
    result: str = ""
    requested_at: str | None = None
    landed_at: str | None = None
    by: str | None = None
    #: The desk session holding this record between `take` and `land`/`fail`.
    taken_by: str | None = None
    taken_at: str | None = None
    #: Why a `fail` refused the landing; the tasks stay built.
    fail_reason: str = ""
    failed_at: str | None = None
    #: How many times a target-moved lease has already sent this
    #: record back. Zero (or absent on an older line) means none;
    #: one is the retry; a second target-moved refusal fails it.
    land_attempts: int = 0


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
    #: The branch the session's worktree is on. A review of an existing
    #: branch records that branch, not a throwaway cut from it.
    branch: str = ""
    log: str = ""
    timeout: str = ""
    launched_by: str | None = None
    #: The vendor's own session id, so the roster names the conversation
    #: this session is. A relaunch starts a fresh one and records it here
    #: under the same Foreman session id, which is how the roster tells a
    #: relaunched supervisor from the one it replaced.
    vendor_session: str | None = None
    #: Headless sessions run one turn per wake and hold no long-lived
    #: process: no window, no workspace, pid None while running. The
    #: collector's liveness branches all require a pid, so a headless
    #: session is never read as dead, silent or stalled for having none.
    headless: bool = False
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
    #: A grant is released by appending a revised copy of its own line, so
    #: it needs an identity to fold on; held slots are the open grants.
    id: str | None = None
    pool: str = ""
    front: str = ""
    role: str = ""
    job: str | None = None
    session: str | None = None
    granted_at: str | None = None
    released_at: str | None = None
    #: Why the slot came back: "returned", "killed", "failed".
    released_because: str = ""


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
    #: The spec the verified job was launched from. A verification is a
    #: claim about a particular piece of work, and the spec is what says
    #: which piece: without it, evidence from a probe job and evidence from
    #: the front's own work read identically on the ledger.
    spec_path: str = ""


@dataclass(frozen=True)
class Finding(Entity):
    _aliases: ClassVar[dict[str, str]] = {"class_": "class"}

    id: str | None = None
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


#: Reasons a wake event names. Job reasons go to the job's launcher,
#: ``inbox answered`` to the session that asked, ``rule landed`` to the
#: supervisor of the rule's front, ``told`` to the named session,
#: ``heartbeat`` to any session with a live turn contract, ``merge
#: requested`` to the front's merge desk, and ``merge landed`` /
#: ``merge failed`` to the supervisor that asked.
WAKE_REASONS = (
    "job returned",
    "job returned-with-work",
    "job failed",
    "job killed",
    "job timed out",
    "inbox answered",
    "rule landed",
    "told",
    "heartbeat",
    "merge requested",
    "merge landed",
    "merge failed",
)


@dataclass(frozen=True)
class WakeEvent(Entity):
    """One wake event for one session, in its own events.jsonl ledger.

    The ledger is append-only and folds last-wins by id like every other
    ledger. Delivery is a revised copy of the same line with
    ``delivered_at`` set, so the fold is what proves an event woke its
    session exactly once. Each reason names only the ids the next task
    needs — never a prose blob — except ``told``, which carries the
    foreman's one sentence.
    """

    _aliases: ClassVar[dict[str, str]] = {"from_": "from"}

    id: str | None = None
    session: str = ""
    reason: str = ""
    at: str | None = None
    job: str | None = None
    front: str | None = None
    task: str | None = None
    inbox: str | None = None
    ruling: str | None = None
    rule: str | None = None
    from_: str | None = None
    text: str | None = None
    merge: str | None = None
    branch: str | None = None
    sha: str | None = None
    delivered_at: str | None = None


@dataclass(frozen=True)
class Digest(Entity):
    at: str | None = None
    text: str = ""
    computed: dict[str, Any] = field(default_factory=dict)
