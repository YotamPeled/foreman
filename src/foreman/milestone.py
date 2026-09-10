"""`foreman milestone add|split|merge|list`: milestones held by the runtime.

``milestone add`` appends one milestone to ``fronts/<name>/milestones.jsonl``.
Only the front's own supervisor (or the owner at a terminal) may write.
The ledger is append-only; the fold derives which milestones are still
live (not split or merged). There is no rename and no delete verb.
"""

from __future__ import annotations

import argparse

from . import caller, cli, entities, fronts, ids, paths, store
from .caller import Refusal

LIVE_LIMIT = 8


def _refuse(violations: list[str]) -> int:
    return Refusal(violations).report()


def _read(front: str) -> tuple[list[dict], list[dict]]:
    raw = store.read_ledger(paths.front_milestones_path(front))
    return raw, _fold(raw)


def _fold(records: list[dict]) -> list[dict]:
    """Last-wins by id, with ``split_into`` / ``merged_into`` derived.

    A later line with ``op=split`` (or ``merge``) and ``from_ids``
    naming a source marks that source on the fold. Live milestones are
    those with neither list set.
    """
    folded = store.fold_by_id(records)
    split_into: dict[str, list[str]] = {}
    merged_into: dict[str, list[str]] = {}
    for record in folded:
        rid = record.get("id")
        if not isinstance(rid, str) or not rid:
            continue
        from_ids = record.get("from_ids")
        from_ids = from_ids if isinstance(from_ids, list) else []
        sources = [item for item in from_ids if isinstance(item, str) and item]
        op = record.get("op")
        target = split_into if op == "split" else (
            merged_into if op == "merge" else None)
        if target is None:
            continue
        for src in sources:
            if rid not in target.setdefault(src, []):
                target[src].append(rid)
    result: list[dict] = []
    for record in folded:
        rid = record.get("id")
        extra = dict(record)
        if isinstance(rid, str):
            if not extra.get("split_into") and rid in split_into:
                extra["split_into"] = list(split_into[rid])
            if not extra.get("merged_into") and rid in merged_into:
                extra["merged_into"] = list(merged_into[rid])
        result.append(extra)
    return result


def _is_live(record: dict) -> bool:
    split = record.get("split_into") or []
    merged = record.get("merged_into") or []
    return not split and not merged


def _live(folded: list[dict]) -> list[dict]:
    return [record for record in folded if _is_live(record)]


def _titles_on_fold(folded: list[dict]) -> set[str]:
    titles: set[str] = set()
    for record in folded:
        title = str(record.get("title") or "").strip()
        if title:
            titles.add(title)
    return titles


def _next_order(live: list[dict]) -> int:
    orders = [record.get("order") for record in live
              if isinstance(record.get("order"), int)
              and not isinstance(record.get("order"), bool)]
    return (max(orders) + 1) if orders else 1


def _commit(*, front: str, op: str, order: int, title: str,
            done_when: str, verify: str, reason: str, break_: str,
            from_ids: list[str], by: str, force: bool = False,
            milestone_id: str | None = None) -> str:
    mid = milestone_id or ids.mint("milestone")
    now = store.utcnow_iso()
    store.append_ledger(
        paths.front_milestones_path(front),
        entities.Milestone(
            id=mid,
            front=front,
            op=op,
            order=order,
            title=title,
            done_when=done_when,
            verify=verify,
            reason=reason,
            break_=break_,
            from_ids=list(from_ids),
            force=force,
            at=now,
            by=by,
        ).to_dict(),
        session_id=by,
    )
    return mid


def milestone_add_main(front: str, title: str | None,
                       done_when: str | None, verify: str | None,
                       reason: str | None, break_: str | None,
                       order: int | None = None,
                       force: bool = False) -> int:
    verb = "milestone add"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    title_text = "" if title is None else str(title).strip()
    done_text = "" if done_when is None else str(done_when).strip()
    verify_text = "" if verify is None else str(verify).strip()
    reason_text = "" if reason is None else str(reason).strip()
    break_text = "" if break_ is None else str(break_).strip()
    if not title_text:
        violations.append("field '--title' is required")
    if not done_text:
        violations.append("field '--done-when' is required")
    if not verify_text:
        violations.append("field '--verify' is required")
    if not reason_text:
        violations.append("field '--reason' is required")
    if not break_text:
        violations.append("field '--break' is required")
    folded: list[dict] = []
    live: list[dict] = []
    if record is not None:
        _raw, folded = _read(front_name)
        live = _live(folded)
        if title_text and title_text in _titles_on_fold(folded):
            violations.append(
                f"title '{title_text}' is already on the fold")
        if len(live) + 1 > LIVE_LIMIT and not force:
            violations.append(
                "more than eight live milestones "
                "(pass --force with --reason to lift)")
        elif len(live) + 1 > LIVE_LIMIT and force and not reason_text:
            violations.append(
                "more than eight live milestones "
                "(pass --force with --reason to lift)")
    if violations:
        return _refuse(violations)
    assert record is not None
    who = caller.by_line(me)
    assigned = order if isinstance(order, int) else _next_order(live)
    mid = _commit(
        front=front_name, op="add", order=assigned, title=title_text,
        done_when=done_text, verify=verify_text, reason=reason_text,
        break_=break_text, from_ids=[], by=who, force=bool(force))
    print(mid)
    return 0


def add_milestone_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="milestone_verb", required=True)
    add = verbs.add_parser(
        "add", help="Append a milestone to the front's ledger.")
    add.add_argument("front", help="front the milestone belongs to")
    add.add_argument("--title", default=None,
                     help="milestone title (required)")
    add.add_argument("--done-when", dest="done_when", default=None,
                     help="the done-when sentence (required)")
    add.add_argument("--verify", default=None,
                     help="the verify command (required)")
    add.add_argument("--reason", default=None,
                     help="why this milestone exists (required)")
    add.add_argument("--break", dest="break_", default=None,
                     help="how a proof of this milestone is broken "
                          "(required)")
    add.add_argument("--order", type=int, default=None,
                     help="sort order (default: next after live)")
    add.add_argument("--force", action="store_true",
                     help="lift the eight-live-milestone cap; "
                          "requires --reason")


@cli.subcommand("milestone", help="Write or read the front's milestones.")
def _milestone_entry(args: argparse.Namespace) -> int:
    if args.milestone_verb == "add":
        return milestone_add_main(
            args.front, args.title, args.done_when, args.verify,
            args.reason, args.break_, order=args.order, force=args.force)
    raise AssertionError(f"unknown milestone verb {args.milestone_verb!r}")


_milestone_entry.add_arguments = add_milestone_arguments  # type: ignore[attr-defined]
