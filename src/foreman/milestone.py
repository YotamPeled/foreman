"""`foreman milestone add|split|merge|list`: milestones held by the runtime.

``milestone add`` appends one milestone to ``fronts/<name>/milestones.jsonl``.
Only the front's own supervisor (or the owner at a terminal) may write.
The ledger is append-only; the fold derives which milestones are still
live (not split or merged). There is no rename and no delete verb.
"""

from __future__ import annotations

import argparse
import json

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


def _change_count(raw: list[dict]) -> int:
    """Split operations plus merge lines.

    One split of a source into N parts writes N ``op=split`` lines that
    share ``from_ids``; that is one change, not N. Each ``op=merge``
    line is one change.
    """
    split_sources: set[str] = set()
    merges = 0
    for record in raw:
        op = record.get("op")
        if op == "split":
            from_ids = record.get("from_ids")
            from_ids = from_ids if isinstance(from_ids, list) else []
            for src in from_ids:
                if isinstance(src, str) and src:
                    split_sources.add(src)
        elif op == "merge":
            merges += 1
    return len(split_sources) + merges


def _pieces_done(front: str, milestone_id: str) -> str:
    """``landed/total`` task nodes under ``milestone_id``, or ``-``.

    The tree ledger is a sibling door; this only reads it when the file
    exists. Job nodes do not count. Missing ``state`` is not landed.
    """
    path = paths.front_tree_path(front)
    try:
        exists = path.exists()
    except OSError:
        return "-"
    if not exists:
        return "-"
    nodes = store.fold_by_id(store.read_ledger(path))
    children: dict[object, list[dict]] = {}
    for node in nodes:
        children.setdefault(node.get("parent"), []).append(node)
    tasks: list[dict] = []
    stack = list(children.get(milestone_id, []))
    while stack:
        node = stack.pop()
        if node.get("kind") == "task":
            tasks.append(node)
        nid = node.get("id")
        if nid is not None:
            stack.extend(children.get(nid, []))
    landed = sum(1 for task in tasks if task.get("state") == "landed")
    return f"{landed}/{len(tasks)}"


def _empty_field(flag: str, value: str | None, violations: list[str]) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        violations.append(f"field '{flag}' is required")
    return text


def _parse_part(raw: str, violations: list[str]
                ) -> tuple[str, str, str, str] | None:
    fields = str(raw).split("|")
    if len(fields) != 4:
        violations.append(
            "field '--part' must be title|done-when|verify|break")
        return None
    title, done_when, verify, break_ = (part.strip() for part in fields)
    if not title:
        violations.append("field '--part' names an empty title")
    if not done_when:
        violations.append("field '--part' names an empty done-when")
    if not verify:
        violations.append("field '--part' names an empty verify")
    if not break_:
        violations.append("field '--part' names an empty break")
    return title, done_when, verify, break_


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


def _gate_front(verb: str, front: str | None
                ) -> tuple[object, list[str], str, dict | None]:
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    return me, violations, front_name, record


def milestone_split_main(front: str, source: str | None,
                         into: list[str] | None, parts: list[str] | None,
                         reason: str | None) -> int:
    verb = "milestone split"
    me, violations, front_name, record = _gate_front(verb, front)
    src_id = "" if source is None else str(source).strip()
    if not src_id:
        violations.append("field 'id' is required")
    titles = [str(item).strip() for item in (into or [])]
    if len([title for title in titles if title]) < 2:
        violations.append("split needs at least two --into titles")
    for item in into or []:
        if not str(item).strip():
            violations.append("field '--into' is required")
            break
    reason_text = _empty_field("--reason", reason, violations)
    parsed: list[tuple[str, str, str, str]] = []
    for raw in parts or []:
        part = _parse_part(raw, violations)
        if part is not None:
            parsed.append(part)
    by_title: dict[str, tuple[str, str, str, str]] = {}
    for part in parsed:
        title = part[0]
        if not title:
            continue
        if title in by_title:
            violations.append(f"title '{title}' is repeated in --part")
        else:
            by_title[title] = part
    for title in titles:
        if title and title not in by_title:
            violations.append(
                f"field '--part' is required for title '{title}'")
    for title in by_title:
        if title not in titles:
            violations.append(f"--part title '{title}' has no --into")
    folded: list[dict] = []
    source_record: dict | None = None
    if record is not None:
        _raw, folded = _read(front_name)
        by_id = {item["id"]: item for item in folded
                 if isinstance(item.get("id"), str)}
        if src_id:
            source_record = by_id.get(src_id)
            if source_record is None:
                violations.append(f"unknown milestone '{src_id}'")
            elif not _is_live(source_record):
                violations.append(f"milestone '{src_id}' is not live")
        existing = _titles_on_fold(folded)
        for title in titles:
            if title and title in existing:
                violations.append(f"title '{title}' is already on the fold")
        n_new = len([title for title in titles if title])
        if source_record is not None and n_new >= 2:
            if len(_live(folded)) - 1 + n_new > LIVE_LIMIT:
                violations.append("more than eight live milestones")
    if violations:
        return _refuse(violations)
    assert record is not None and source_record is not None
    who = caller.by_line(me)
    base = source_record.get("order")
    base_order = base if isinstance(base, int) and not isinstance(base, bool) else 1
    new_ids: list[str] = []
    for offset, title in enumerate(titles):
        part = by_title[title]
        new_ids.append(_commit(
            front=front_name, op="split", order=base_order + offset,
            title=part[0], done_when=part[1], verify=part[2],
            reason=reason_text, break_=part[3], from_ids=[src_id],
            by=who))
    print(" ".join(new_ids))
    return 0


def milestone_merge_main(front: str, ids: list[str] | None,
                         title: str | None, done_when: str | None,
                         verify: str | None, break_: str | None,
                         reason: str | None) -> int:
    verb = "milestone merge"
    me, violations, front_name, record = _gate_front(verb, front)
    source_ids = [str(item).strip() for item in (ids or [])]
    source_ids = [item for item in source_ids if item]
    if len(source_ids) < 2:
        violations.append("merge needs at least two milestone ids")
    if len(source_ids) != len(set(source_ids)):
        violations.append("merge names a milestone more than once")
    title_text = _empty_field("--title", title, violations)
    done_text = _empty_field("--done-when", done_when, violations)
    verify_text = _empty_field("--verify", verify, violations)
    reason_text = _empty_field("--reason", reason, violations)
    break_text = _empty_field("--break", break_, violations)
    folded: list[dict] = []
    sources: list[dict] = []
    if record is not None:
        _raw, folded = _read(front_name)
        by_id = {item["id"]: item for item in folded
                 if isinstance(item.get("id"), str)}
        for src_id in source_ids:
            found = by_id.get(src_id)
            if found is None:
                violations.append(f"unknown milestone '{src_id}'")
            elif not _is_live(found):
                violations.append(f"milestone '{src_id}' is not live")
            else:
                sources.append(found)
        if title_text and title_text in _titles_on_fold(folded):
            violations.append(
                f"title '{title_text}' is already on the fold")
    if violations:
        return _refuse(violations)
    assert record is not None
    who = caller.by_line(me)
    orders = [item.get("order") for item in sources
              if isinstance(item.get("order"), int)
              and not isinstance(item.get("order"), bool)]
    assigned = min(orders) if orders else 1
    mid = _commit(
        front=front_name, op="merge", order=assigned, title=title_text,
        done_when=done_text, verify=verify_text, reason=reason_text,
        break_=break_text, from_ids=source_ids, by=who)
    print(mid)
    return 0


def milestone_list_main(front: str, as_json: bool = False,
                        history: bool = False) -> int:
    verb = "milestone list"
    _me, violations, front_name, record = _gate_front(verb, front)
    if violations:
        return _refuse(violations)
    assert record is not None
    raw, folded = _read(front_name)
    live = sorted(
        _live(folded),
        key=lambda item: (
            item.get("order") if isinstance(item.get("order"), int)
            and not isinstance(item.get("order"), bool) else 0,
            str(item.get("id") or ""),
        ),
    )
    change = _change_count(raw)
    pieces = {
        str(item["id"]): _pieces_done(front_name, str(item["id"]))
        for item in live if isinstance(item.get("id"), str)
    }
    if as_json:
        payload: dict = {
            "milestones": [
                {**item, "pieces_done": pieces.get(str(item.get("id")), "-")}
                for item in live
            ],
            "change_count": change,
        }
        if history:
            payload["history"] = list(raw)
        print(json.dumps(payload))
        return 0
    lines = [
        f"{item.get('id')}  {item.get('order')}  {item.get('title')}  "
        f"pieces done {pieces.get(str(item.get('id')), '-')}"
        for item in live
    ]
    lines.append(f"change count {change}")
    if history:
        lines.append("history:")
        for item in raw:
            lines.append(
                f"{item.get('id')}  {item.get('op')}  {item.get('title')}")
    print("\n".join(lines))
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
    split = verbs.add_parser(
        "split", help="Split one live milestone into two or more.")
    split.add_argument("front", help="front the milestone belongs to")
    split.add_argument("id", help="milestone to split")
    split.add_argument("--into", action="append", default=None,
                       help="title of a new part (repeatable, at least two)")
    split.add_argument(
        "--part", action="append", default=None,
        help="title|done-when|verify|break for one part (repeatable)")
    split.add_argument("--reason", default=None,
                       help="why the split (required)")
    merge = verbs.add_parser(
        "merge", help="Merge live milestones into one.")
    merge.add_argument("front", help="front the milestones belong to")
    merge.add_argument("ids", nargs="+",
                       help="live milestone ids to merge (at least two)")
    merge.add_argument("--title", default=None,
                       help="title of the merged milestone (required)")
    merge.add_argument("--done-when", dest="done_when", default=None,
                       help="the done-when sentence (required)")
    merge.add_argument("--verify", default=None,
                       help="the verify command (required)")
    merge.add_argument("--break", dest="break_", default=None,
                       help="how a proof of this milestone is broken "
                            "(required)")
    merge.add_argument("--reason", default=None,
                       help="why the merge (required)")
    listing = verbs.add_parser(
        "list", help="Print the front's live milestones.")
    listing.add_argument("front", help="front whose milestones to print")
    listing.add_argument("--json", dest="as_json", action="store_true",
                         help="print the live list as JSON")
    listing.add_argument("--history", action="store_true",
                         help="print every ledger line under the live list")


@cli.subcommand("milestone", help="Write or read the front's milestones.")
def _milestone_entry(args: argparse.Namespace) -> int:
    if args.milestone_verb == "add":
        return milestone_add_main(
            args.front, args.title, args.done_when, args.verify,
            args.reason, args.break_, order=args.order, force=args.force)
    if args.milestone_verb == "split":
        return milestone_split_main(
            args.front, args.id, args.into, args.part, args.reason)
    if args.milestone_verb == "merge":
        return milestone_merge_main(
            args.front, args.ids, args.title, args.done_when, args.verify,
            args.break_, args.reason)
    if args.milestone_verb == "list":
        return milestone_list_main(
            args.front, as_json=args.as_json, history=args.history)
    raise AssertionError(f"unknown milestone verb {args.milestone_verb!r}")


_milestone_entry.add_arguments = add_milestone_arguments  # type: ignore[attr-defined]
