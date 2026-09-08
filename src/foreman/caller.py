"""Caller identity for foreman verbs.

Every verb resolves its caller here, once: the calling session comes from
the ``FOREMAN_SESSION`` environment variable and is looked up on the roster.
No session in the environment means the owner calling from a terminal.

Two refusals live here, both returned by name:
- unknown session id: the design's "unregistered writer". It also appends an
  anomaly line so the collector can see the attempt.
- a role calling a verb outside its row of the section 12 verb table.

Field validation lives with each verb, but every refusal funnels through
:meth:`Refusal.report` so a call wrong in several ways names every violated
field in one message, never the first one only.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from . import entities, paths, store

SESSION_ENV = "FOREMAN_SESSION"

OWNER = "owner"
FOREMAN = "foreman"
SUPERVISOR = "supervisor"
MERGE_DESK = "merge-desk"

#: Minted ids look like ``rul-a1b2c3d``; anything shaped like that and nothing
#: else is "nothing but an id" for the self-contained check.
_ID_RE = re.compile(r"[a-z]{3}-[a-z2-7]{7}")


class Refusal(Exception):
    """A refused call. Carries every violation so all are named at once."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = list(violations)

    def report(self) -> int:
        print(f"foreman: refused: {'; '.join(self.violations)}", file=sys.stderr)
        return 1


@dataclass
class Caller:
    role: str
    session_id: str | None
    session: dict


def read_roster() -> dict:
    roster = store.read_snapshot(paths.roster_path(), default=None)
    if not isinstance(roster, dict):
        return {"sessions": {}}
    sessions = roster.get("sessions")
    if not isinstance(sessions, dict):
        roster["sessions"] = {}
    return roster


def _record_unregistered_writer(session_id: str, verb: str) -> None:
    """Record one open anomaly per unknown session, not one per refusal."""
    try:
        for line in store.read_ledger(paths.anomalies_path()):
            if line.get("kind") == "unregistered writer" and \
                    line.get("subject") == session_id and \
                    line.get("resolved_at") is None:
                return
    except OSError:
        pass
    anomaly = entities.Anomaly(
        kind="unregistered writer",
        subject=session_id,
        since=store.utcnow_iso(),
        detail=f"refused '{verb}' from unknown session '{session_id}'",
    )
    store.append_ledger(paths.anomalies_path(), anomaly.to_dict())


def resolve(verb: str) -> tuple[Caller | None, list[str]]:
    """Resolve the caller for ``verb``.

    Returns the caller (None when the roster does not know the session) plus
    a list of identity violations, empty for the owner and known sessions.
    An unknown session is recorded as an "unregistered writer" anomaly so the
    collector can see the attempt.
    """
    session_id = os.environ.get(SESSION_ENV)
    if not session_id:
        return Caller(role=OWNER, session_id=None, session={}), []
    sessions = read_roster().get("sessions", {})
    session = sessions.get(session_id)
    if not isinstance(session, dict):
        _record_unregistered_writer(session_id, verb)
        return None, [f"unknown session '{session_id}' (unregistered writer)"]
    return (
        Caller(role=session.get("role", ""), session_id=session_id, session=session),
        [],
    )


def check_role(
    me: Caller | None, verb: str, *allowed: str, violations: list[str]
) -> None:
    """Append a role violation unless the caller may call ``verb``.

    The owner is never refused. A caller the roster does not know has no
    role to judge, so identity refusal covers it.
    """
    if me is None or me.role == OWNER or me.role in allowed:
        return
    violations.append(f"role '{me.role}' may not call '{verb}'")


def check_front_supervisor(
    me: Caller | None, front: str | None, verb: str, *, violations: list[str]
) -> None:
    """Append a violation unless the caller supervises ``front``.

    The progress verbs move another session's work, so a role check is not
    enough: the roster must say this session is the supervisor of the front
    the record belongs to. The owner is never refused.
    """
    if me is None or me.role == OWNER:
        return
    if me.role != SUPERVISOR:
        violations.append(f"role '{me.role}' may not call '{verb}'")
        return
    mine = me.session.get("front")
    if mine != front:
        violations.append(
            f"session '{me.session_id}' supervises "
            f"'{mine or '(no front)'}', not '{front}'"
        )


def by_line(me: Caller | None) -> str:
    return me.session_id if me is not None and me.session_id else OWNER


def check_self_contained(value: str, what: str) -> None:
    """Warn on stderr when an owner-facing sentence is not self-contained.

    Never refuses: empty, longer than one line, or nothing but an id still
    gets written; the warning just says so.
    """
    text = value if isinstance(value, str) else ""
    stripped = text.strip()
    if not stripped:
        reason = "empty"
    elif "\n" in text:
        reason = "longer than one line"
    elif _ID_RE.fullmatch(stripped):
        reason = "nothing but an id"
    else:
        return
    print(
        f"foreman: warning: {what} is not self-contained ({reason}); "
        "writing it anyway",
        file=sys.stderr,
    )
