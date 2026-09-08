"""Rulings, inbox and checkpoint verbs.

``rule`` appends swarm/front rulings and records acks; ``ask`` files a
question with a recommendation; ``answer`` records the answer and, in the
same call, turns it into a ruling on the asker's front; ``inbox`` lists
open questions oldest first; ``checkpoint`` writes the calling session's
checkpoint file and stamps the roster's last declared write.

Ledgers stay append-only: acks and answers are appended as revised copies of
their record and folded last-wins at read time, so no byte is ever edited.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone

from . import caller, cli, entities, ids, paths, store
from .caller import FOREMAN, OWNER, SUPERVISOR, Refusal

_FRONT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _fold(records: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """The ledger fold from :mod:`foreman.store`, also keyed by id."""
    folded = store.fold_by_id(records)
    return folded, {record["id"]: record for record in folded}


def read_rulings() -> tuple[list[dict], dict[str, dict]]:
    return _fold(store.read_ledger(paths.rulings_path()))


def read_inbox() -> tuple[list[dict], dict[str, dict]]:
    return _fold(store.read_ledger(paths.inbox_path()))


def _age(asked_at: str | None) -> str:
    if not asked_at:
        return "?"
    try:
        then = datetime.fromisoformat(asked_at)
    except ValueError:
        return "?"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _refuse(violations: list[str]) -> int:
    return Refusal(violations).report()


def add_rule_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("scope", help="swarm, a front name, 'ack' or 'list'")
    sub.add_argument("text", nargs="*", help="ruling text, or the id for ack")


@cli.subcommand("rule", help="Append or ack a ruling, or list the ledger.")
def _rule_entry(args: argparse.Namespace) -> int:
    return rule_main(args.scope, args.text or [])


_rule_entry.add_arguments = add_rule_arguments  # type: ignore[attr-defined]


def rule_main(scope: str, parts: list[str]) -> int:
    me, violations = caller.resolve("rule")
    text = " ".join(parts)
    if scope == "list":
        caller.check_role(me, "rule list", FOREMAN, SUPERVISOR, violations=violations)
        if parts:
            violations.append("field 'text' takes no value with 'rule list'")
        if violations:
            return _refuse(violations)
        for record in read_rulings()[0]:
            ruling = entities.Ruling.from_dict(record)
            acks = " ".join(ruling.acks) if ruling.acks else "-"
            print(f"{ruling.id} [{ruling.scope}] {ruling.text} "
                  f"(source {ruling.source}; acks: {acks})")
        return 0
    if scope == "ack":
        caller.check_role(me, "rule ack", FOREMAN, SUPERVISOR, violations=violations)
        if not parts:
            violations.append("field 'id' is required for 'rule ack'")
        elif text.strip() not in read_rulings()[1]:
            violations.append(f"unknown ruling '{text.strip()}'")
        if violations:
            return _refuse(violations)
        rid = text.strip()
        ruling = entities.Ruling.from_dict(read_rulings()[1][rid])
        who = caller.by_line(me)
        acks = list(ruling.acks) + ([who] if who not in ruling.acks else [])
        store.append_ledger(
            paths.rulings_path(),
            entities.Ruling(
                id=ruling.id, scope=ruling.scope, text=ruling.text,
                source=ruling.source, acks=acks,
            ).to_dict(),
            session_id=who,
        )
        print(f"{rid} acked by {who}")
        return 0
    caller.check_role(me, "rule", FOREMAN, violations=violations)
    if scope != "swarm" and not _FRONT_RE.fullmatch(scope):
        violations.append(
            f"field 'scope' must be 'swarm' or a front name (got '{scope}')"
        )
    if not parts:
        violations.append("field 'text' is required for 'rule <scope> <text>'")
    if violations:
        return _refuse(violations)
    caller.check_self_contained(text, "ruling text")
    who = caller.by_line(me)
    rid = ids.mint("ruling")
    source = "owner" if (me is None or me.role == OWNER) else "foreman"
    store.append_ledger(
        paths.rulings_path(),
        entities.Ruling(id=rid, scope=scope, text=text,
                        source=source, acks=[]).to_dict(),
        session_id=who,
    )
    print(rid)
    return 0


def add_ask_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("question", nargs="*",
                     help="the question for the owner")
    sub.add_argument("--kind", default=None,
                     help="money, irreversible, scope or error")
    sub.add_argument("--recommend", default=None,
                     help="recommended answer (required)")
    sub.add_argument("--option", action="append", default=[],
                     help="an acceptable option; repeatable")


@cli.subcommand("ask", help="File a question to the inbox.")
def _ask_entry(args: argparse.Namespace) -> int:
    return ask_main(args.question or [], args.kind,
                    args.recommend, args.option or [])


_ask_entry.add_arguments = add_ask_arguments  # type: ignore[attr-defined]


def ask_main(question_parts: list[str], kind: str | None,
             recommendation: str | None, options: list[str]) -> int:
    me, violations = caller.resolve("ask")
    caller.check_role(me, "ask", SUPERVISOR, violations=violations)
    question = " ".join(question_parts)
    if not question_parts:
        violations.append("field 'question' is required")
    if kind not in entities.INBOX_KINDS:
        violations.append(
            "field 'kind' must be one of "
            + ", ".join(entities.INBOX_KINDS)
            + (f" (got '{kind}')" if kind else " (missing)")
        )
    if not (recommendation or "").strip():
        violations.append("field 'recommendation' is required")
    if violations:
        return _refuse(violations)
    caller.check_self_contained(question, "question")
    who = caller.by_line(me)
    iid = ids.mint("inbox")
    now = store.utcnow_iso()
    store.append_ledger(
        paths.inbox_path(),
        entities.InboxItem(
            id=iid, from_=who, kind=kind or "",
            question=question, recommendation=(recommendation or "").strip(),
            options=list(options), asked_at=now,
        ).to_dict(),
        session_id=who,
    )
    print(iid)
    return 0


def add_answer_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("id", help="inbox item to answer")
    sub.add_argument("answer", nargs="*", help="answer text or option")


@cli.subcommand("answer", help="Answer an inbox item; the answer becomes a ruling.")
def _answer_entry(args: argparse.Namespace) -> int:
    return answer_main(args.id, args.answer or [])


_answer_entry.add_arguments = add_answer_arguments  # type: ignore[attr-defined]


def answer_main(iid: str, answer_parts: list[str]) -> int:
    me, violations = caller.resolve("answer")
    caller.check_role(me, "answer", FOREMAN, violations=violations)
    by_id = read_inbox()[1]
    record = by_id.get(iid.strip()) if iid else None
    if not (iid or "").strip():
        violations.append("field 'id' is required")
    elif record is None:
        violations.append(f"unknown inbox item '{iid.strip()}'")
    elif entities.InboxItem.from_dict(record).answered_at is not None:
        violations.append(f"inbox item '{iid.strip()}' is already answered")
    answer = " ".join(answer_parts)
    if not answer_parts:
        violations.append("field 'answer' is required")
    if violations:
        return _refuse(violations)
    assert record is not None
    item = entities.InboxItem.from_dict(record)
    who = caller.by_line(me)
    now = store.utcnow_iso()
    store.append_ledger(
        paths.inbox_path(),
        entities.InboxItem(
            id=item.id, from_=item.from_, kind=item.kind,
            question=item.question, recommendation=item.recommendation,
            options=item.options, asked_at=item.asked_at,
            answered_at=now, answer=answer, answered_by=who,
            relayed=item.relayed,
        ).to_dict(),
        session_id=who,
    )
    asker_front = None
    if item.from_ and item.from_ != OWNER:
        asker = caller.read_roster().get("sessions", {}).get(item.from_, {})
        if isinstance(asker, dict):
            asker_front = asker.get("front")
    scope = asker_front or "swarm"
    caller.check_self_contained(answer, "ruling text")
    source = "owner" if (me is None or me.role == OWNER) else "foreman"
    rid = ids.mint("ruling")
    store.append_ledger(
        paths.rulings_path(),
        entities.Ruling(id=rid, scope=scope, text=answer,
                        source=source, acks=[]).to_dict(),
        session_id=who,
    )
    print(f"{item.id} answered; ruling {rid}")
    return 0


@cli.subcommand("inbox", help="List open inbox items, oldest first.")
def _inbox_entry(args: argparse.Namespace) -> int:
    return inbox_main()


def inbox_main() -> int:
    me, violations = caller.resolve("inbox")
    caller.check_role(me, "inbox", FOREMAN, violations=violations)
    if violations:
        return _refuse(violations)
    for record in read_inbox()[0]:
        item = entities.InboxItem.from_dict(record)
        if item.answered_at is not None:
            continue
        print(f"{item.id} [{item.kind}] {_age(item.asked_at)} {item.question} "
              f"-- recommend: {item.recommendation} (from {item.from_})")
    return 0


def add_checkpoint_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--doing", default=None, help="what the session is doing")
    sub.add_argument("--next", default=None, help="what the session does next")
    sub.add_argument("--held", nargs="*", default=[],
                     help="what the session holds")
    sub.add_argument("--waiting", nargs="*", default=[],
                     help="what the session is waiting on")


@cli.subcommand("checkpoint", help="Write the calling session's checkpoint.")
def _checkpoint_entry(args: argparse.Namespace) -> int:
    return checkpoint_main(args.doing, args.next,
                           args.held or [], args.waiting or [])


_checkpoint_entry.add_arguments = add_checkpoint_arguments  # type: ignore[attr-defined]


def checkpoint_main(doing: str | None, next: str | None,
                    held: list[str], waiting: list[str]) -> int:
    me, violations = caller.resolve("checkpoint")
    caller.check_role(me, "checkpoint", SUPERVISOR, FOREMAN,
                      violations=violations)
    if me is not None and me.session_id is None:
        violations.append(
            "field 'session' is required "
            "(the owner has no session checkpoint)"
        )
    if doing is None:
        violations.append("field 'doing' is required")
    if next is None:
        violations.append("field 'next' is required")
    if violations:
        return _refuse(violations)
    assert me is not None and me.session_id is not None
    caller.check_self_contained(doing or "", "checkpoint doing")
    store.write_snapshot(
        paths.checkpoint_path(me.session_id),
        entities.Checkpoint(
            session=me.session_id, doing=(doing or "").strip(),
            next=(next or "").strip(), held=list(held),
            questions=list(waiting),
        ).to_dict(),
    )
    session_id = me.session_id

    def stamp(roster):
        if not isinstance(roster, dict):
            roster = {"sessions": {}}
        sessions = roster.setdefault("sessions", {})
        if not isinstance(sessions, dict):
            sessions = roster["sessions"] = {}
        entry = sessions.setdefault(session_id, {})
        entry["last_declared_at"] = store.utcnow_iso()
        return roster

    store.update_snapshot(paths.roster_path(), stamp, default={"sessions": {}})
    print(f"checkpoint {me.session_id}")
    return 0
