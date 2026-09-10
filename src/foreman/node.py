"""`foreman node add|revise|list`: the tree's one write door.

``node add`` appends one node to ``fronts/<name>/tree.jsonl``. Only the
front's own supervisor (or the owner at a terminal) may write. The door
refuses every violation at once and names each rejected field.
``node revise`` appends a revised copy with ``op = revise``. The ledger
is append-only and folded last-wins on read. ``node list`` prints the
fold indented by depth.
"""

from __future__ import annotations

import argparse
import json

from . import caller, cli, entities, fronts, ids, paths, store
from .caller import Refusal
from .entities import CHILDLESS_NODE_KINDS, NODE_KINDS, NODE_SCOPES

MAX_DEPTH = 3


def _refuse(violations: list[str]) -> int:
    return Refusal(violations).report()


def _read_nodes(front: str) -> tuple[list[dict], dict[str, dict]]:
    folded = store.fold_by_id(store.read_ledger(paths.front_tree_path(front)))
    return folded, {record["id"]: record for record in folded
                    if isinstance(record.get("id"), str)}


def _repo_names(record: dict) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    repos = record.get("repositories")
    repos = repos if isinstance(repos, list) else []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        name = str(repo.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _unknown_repo_violation(record: dict, front: str, repo: str) -> str | None:
    if record.get("shape") != "v5":
        return None
    known = _repo_names(record)
    if repo in known:
        return None
    listed = ", ".join(known) if known else "(none)"
    return (f"unknown repository '{repo}' on front "
            f"'{front}' (known: {listed})")


def _depth_of(node_id: str, by_id: dict[str, dict], front_name: str) -> int:
    """1 for a milestone (parent is the front name), else 1 + parent's depth."""
    seen: set[str] = set()
    depth = 0
    current: str | None = node_id
    while current:
        if current in seen:
            break
        seen.add(current)
        record = by_id.get(current)
        if record is None:
            break
        depth += 1
        parent = str(record.get("parent") or "")
        if parent == front_name or parent == str(record.get("front") or ""):
            return depth
        current = parent
    return depth


def _new_depth(kind: str, parent: str, by_id: dict[str, dict],
               front_name: str) -> int:
    if kind == "milestone" and parent == front_name:
        return 1
    if parent == front_name:
        return 1
    parent_node = by_id.get(parent)
    if parent_node is None:
        return 0
    return 1 + _depth_of(parent, by_id, front_name)


def _required(flag: str, value: str | None, violations: list[str]) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        violations.append(f"field '{flag}' is required")
    return text


def node_add_main(
        front: str, parent: str | None, kind: str | None,
        title: str | None, verify: str | None,
        must_not_touch: str | None, reason: str | None,
        break_: str | None, repo: str | None,
        what: str | None = None, property: str | None = None,
        scope: str | None = None, role: str | None = None,
        after: list[str] | None = None, source: str | None = None,
        mechanical: bool = False, node_id: str | None = None,
        ) -> int:
    verb = "node add"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    parent_text = _required("--parent", parent, violations)
    kind_text = "" if kind is None else str(kind).strip()
    if not kind_text:
        violations.append("field '--kind' is required")
    elif kind_text not in NODE_KINDS:
        violations.append(
            "field '--kind' must be milestone, task, job or derived")
    title_text = _required("--title", title, violations)
    verify_text = _required("--verify", verify, violations)
    must_text = _required("--must-not-touch", must_not_touch, violations)
    reason_text = _required("--reason", reason, violations)
    break_text = _required("--break", break_, violations)
    repo_name = _required("--repo", repo, violations)
    if repo_name and record is not None:
        unknown = _unknown_repo_violation(record, front_name, repo_name)
        if unknown:
            violations.append(unknown)
    what_text = "" if what is None else str(what)
    property_text = "" if property is None else str(property).strip()
    scope_text = "" if scope is None else str(scope).strip()
    if not scope_text:
        scope_text = "source-test"
    elif scope_text not in NODE_SCOPES:
        violations.append(
            "field '--scope' must be source-test, fixture, live "
            "or production-load")
    role_text = "" if role is None else str(role).strip()
    source_text = "" if source is None else str(source).strip()
    if kind_text == "derived" and not source_text:
        violations.append("field '--source' is required")
    after_ids = [str(item).strip() for item in (after or [])]
    after_ids = [item for item in after_ids if item]
    for raw in after or []:
        if not str(raw).strip():
            violations.append("field '--after' names an empty node id")
    given_id = "" if node_id is None else str(node_id).strip()
    by_id: dict[str, dict] = {}
    if record is not None:
        _folded, by_id = _read_nodes(front_name)
        if given_id and given_id in by_id:
            violations.append(f"id '{given_id}' is already used")
        if kind_text == "milestone":
            if parent_text and parent_text != front_name:
                violations.append(
                    f"field '--parent' must equal the front name "
                    f"'{front_name}'")
        elif parent_text:
            parent_node = by_id.get(parent_text)
            if parent_node is None:
                violations.append(
                    f"field '--parent' is not a node on front "
                    f"'{front_name}'")
            else:
                parent_kind = str(parent_node.get("kind") or "")
                if parent_kind in CHILDLESS_NODE_KINDS:
                    violations.append(
                        f"field '--parent' is kind '{parent_kind}' "
                        f"and takes no children")
        if parent_text and kind_text:
            depth = _new_depth(kind_text, parent_text, by_id, front_name)
            if depth > MAX_DEPTH:
                violations.append("field 'depth' is past 3")
        for after_id in after_ids:
            if after_id not in by_id:
                violations.append(
                    f"field '--after' names unknown node '{after_id}'")
    if violations:
        return _refuse(violations)
    assert record is not None
    who = caller.by_line(me)
    now = store.utcnow_iso()
    nid = given_id or ids.mint("node")
    store.append_ledger(
        paths.front_tree_path(front_name),
        entities.Node(
            id=nid,
            front=front_name,
            parent=parent_text,
            kind=kind_text,
            title=title_text,
            repo=repo_name,
            what=what_text,
            verify=verify_text,
            must_not_touch=must_text,
            reason=reason_text,
            break_=break_text,
            property=property_text,
            scope=scope_text,
            role=role_text,
            after=list(after_ids),
            source=source_text,
            mechanical=bool(mechanical),
            op="",
            at=now,
            by=who,
        ).to_dict(),
        session_id=who,
    )
    print(nid)
    return 0


def node_revise_main(
        front: str, node_id: str | None, reason: str | None,
        parent: str | None = None, kind: str | None = None,
        title: str | None = None, verify: str | None = None,
        must_not_touch: str | None = None, break_: str | None = None,
        repo: str | None = None, what: str | None = None,
        property: str | None = None, scope: str | None = None,
        role: str | None = None, after: list[str] | None = None,
        source: str | None = None, mechanical: bool | None = None,
        state: str | None = None,
        ) -> int:
    verb = "node revise"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    nid = "" if node_id is None else str(node_id).strip()
    if not nid:
        violations.append("field 'id' is required")
    reason_text = _required("--reason", reason, violations)
    if parent is not None:
        violations.append("field '--parent' may not change")
    if kind is not None:
        violations.append("field '--kind' may not change")
    scope_text = None if scope is None else str(scope).strip()
    if scope_text == "":
        scope_text = None
    if scope_text is not None and scope_text not in NODE_SCOPES:
        violations.append(
            "field '--scope' must be source-test, fixture, live "
            "or production-load")
    by_id: dict[str, dict] = {}
    existing: dict | None = None
    if record is not None:
        _folded, by_id = _read_nodes(front_name)
        if nid:
            existing = by_id.get(nid)
            if existing is None:
                violations.append(f"unknown node '{nid}'")
        after_ids = None
        if after is not None:
            after_ids = [str(item).strip() for item in after]
            after_ids = [item for item in after_ids if item]
            for after_id in after_ids:
                if after_id not in by_id:
                    violations.append(
                        f"field '--after' names unknown node '{after_id}'")
        repo_name = None if repo is None else str(repo).strip()
        if repo_name:
            unknown = _unknown_repo_violation(record, front_name, repo_name)
            if unknown:
                violations.append(unknown)
    if violations:
        return _refuse(violations)
    assert record is not None and existing is not None
    who = caller.by_line(me)
    now = store.utcnow_iso()
    updated = dict(existing)
    if title is not None:
        updated["title"] = str(title).strip()
    if verify is not None:
        updated["verify"] = str(verify).strip()
    if must_not_touch is not None:
        updated["must_not_touch"] = str(must_not_touch).strip()
    if break_ is not None:
        updated["break"] = str(break_).strip()
    if repo is not None:
        updated["repo"] = str(repo).strip()
    if what is not None:
        updated["what"] = str(what)
    if property is not None:
        updated["property"] = str(property).strip()
    if scope_text is not None:
        updated["scope"] = scope_text
    if role is not None:
        updated["role"] = str(role).strip()
    if after is not None:
        updated["after"] = [
            str(item).strip() for item in after if str(item).strip()]
    if source is not None:
        updated["source"] = str(source).strip()
    if mechanical:
        updated["mechanical"] = True
    if state is not None:
        updated["state"] = str(state).strip()
    updated["op"] = "revise"
    updated["reason_revised"] = reason_text
    updated["at"] = now
    updated["by"] = who
    # Rebuild through the entity so unknown keys stay dropped and
    # aliases (break) stay on the wire.
    line = entities.Node.from_dict(updated).to_dict()
    line["op"] = "revise"
    line["reason_revised"] = reason_text
    line["at"] = now
    line["by"] = who
    store.append_ledger(
        paths.front_tree_path(front_name), line, session_id=who)
    print(nid)
    return 0


def _children_of(folded: list[dict]) -> dict[object, list[dict]]:
    children: dict[object, list[dict]] = {}
    for node in folded:
        children.setdefault(node.get("parent"), []).append(node)
    return children


def _walk_tree(folded: list[dict], front_name: str,
               under: str | None = None
               ) -> tuple[list[tuple[dict, int]] | None, dict[str, dict]]:
    """Preorder (node, depth) pairs. None means ``under`` is unknown."""
    by_id = {record["id"]: record for record in folded
             if isinstance(record.get("id"), str)}
    children = _children_of(folded)

    def emit(node: dict, depth: int) -> list[tuple[dict, int]]:
        rows = [(node, depth)]
        nid = node.get("id")
        for child in children.get(nid, []):
            rows.extend(emit(child, depth + 1))
        return rows

    if under is not None:
        root = by_id.get(under)
        if root is None:
            return None, by_id
        depth = _depth_of(under, by_id, front_name)
        return emit(root, depth), by_id

    placed: set[str] = set()
    rows: list[tuple[dict, int]] = []

    def mark(node: dict) -> None:
        nid = node.get("id")
        if isinstance(nid, str):
            placed.add(nid)
        for child in children.get(nid, []):
            mark(child)

    for root in children.get(front_name, []):
        rows.extend(emit(root, 1))
        mark(root)
    for node in folded:
        nid = node.get("id")
        if isinstance(nid, str) and nid not in placed:
            rows.extend(emit(node, 1))
            mark(node)
    return rows, by_id


def _job_counts(task_id: object, children: dict[object, list[dict]]
                ) -> tuple[int, int]:
    """Landed job children over all job children of a task."""
    jobs = [child for child in children.get(task_id, [])
            if child.get("kind") == "job"]
    landed = sum(1 for job in jobs if job.get("state") == "landed")
    return landed, len(jobs)


def _format_node(node: dict, depth: int,
                 children: dict[object, list[dict]]) -> str:
    indent = "  " * max(depth - 1, 0)
    line = f"{indent}{node.get('id')}  {node.get('kind')}  {node.get('title')}"
    if node.get("kind") == "task":
        landed, total = _job_counts(node.get("id"), children)
        line = f"{line}  {landed}/{total}"
    return line


def node_list_main(front: str, under: str | None = None,
                   as_json: bool = False) -> int:
    verb = "node list"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    under_id = None if under is None else str(under).strip()
    if under is not None and not under_id:
        violations.append("field '--under' is required")
        under_id = None
    folded: list[dict] = []
    rows = None
    if record is not None and not violations:
        folded, _by_id = _read_nodes(front_name)
        rows, _ = _walk_tree(folded, front_name, under=under_id)
        if under_id and rows is None:
            violations.append(
                f"field '--under' names unknown node '{under_id}'")
    if violations:
        return _refuse(violations)
    assert record is not None and rows is not None
    if as_json:
        if under_id:
            print(json.dumps([node for node, _depth in rows]))
        else:
            print(json.dumps(folded))
        return 0
    if not rows:
        print("(no tree yet)")
        return 0
    children = _children_of(folded)
    print("\n".join(_format_node(node, depth, children)
                    for node, depth in rows))
    return 0


def add_node_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="node_verb", required=True)
    add = verbs.add_parser("add", help="Append a node to the front's tree.")
    add.add_argument("front", help="front the node belongs to")
    add.add_argument("--parent", default=None,
                     help="front name for a milestone, else a node id "
                          "(required)")
    add.add_argument("--kind", default=None,
                     help="milestone, task, job or derived (required)")
    add.add_argument("--title", default=None,
                     help="node title (required)")
    add.add_argument("--verify", default=None,
                     help="the verify command (required)")
    add.add_argument("--must-not-touch", dest="must_not_touch", default=None,
                     help="what this node must not touch (required)")
    add.add_argument("--reason", default=None,
                     help="why this node exists (required)")
    add.add_argument("--break", dest="break_", default=None,
                     help="the one-line change that must make verify "
                          "go red (required)")
    add.add_argument("--repo", default=None,
                     help="repository name (required)")
    add.add_argument("--what", default=None,
                     help="unbounded body of the node")
    add.add_argument("--property", default=None,
                     help="what a wrong answer looks like")
    add.add_argument("--scope", default=None,
                     help="source-test, fixture, live or production-load "
                          "(default: source-test)")
    add.add_argument("--role", default=None,
                     help="role this node is for")
    add.add_argument("--after", action="append", default=None,
                     help="node id this one waits on (repeatable)")
    add.add_argument("--source", default=None,
                     help="node or file a derived node comes from "
                          "(required for derived)")
    add.add_argument("--mechanical", action="store_true",
                     help="this node is a mechanical leaf")
    add.add_argument("--id", dest="node_id", default=None,
                     help="id to use instead of a minted nod- id")
    revise = verbs.add_parser(
        "revise", help="Append a revised copy of a tree node.")
    revise.add_argument("front", help="front the node belongs to")
    revise.add_argument("id", help="node to revise")
    revise.add_argument("--reason", default=None,
                        help="why this revision (required)")
    revise.add_argument("--parent", default=None,
                        help="refused: parent may not change")
    revise.add_argument("--kind", default=None,
                        help="refused: kind may not change")
    revise.add_argument("--title", default=None, help="new title")
    revise.add_argument("--verify", default=None, help="new verify command")
    revise.add_argument("--must-not-touch", dest="must_not_touch",
                        default=None, help="new must-not-touch")
    revise.add_argument("--break", dest="break_", default=None,
                        help="new break")
    revise.add_argument("--repo", default=None, help="new repository")
    revise.add_argument("--what", default=None, help="new body")
    revise.add_argument("--property", default=None, help="new property")
    revise.add_argument("--scope", default=None, help="new evidence scope")
    revise.add_argument("--role", default=None, help="new role")
    revise.add_argument("--after", action="append", default=None,
                        help="replace the after list (repeatable)")
    revise.add_argument("--source", default=None, help="new source")
    revise.add_argument("--mechanical", action="store_true",
                        help="mark the node mechanical")
    revise.add_argument("--state", default=None,
                        help="set state (landed, until landing lands)")
    listing = verbs.add_parser(
        "list", help="Print the front's folded tree.")
    listing.add_argument("front", help="front whose tree to print")
    listing.add_argument("--under", default=None,
                         help="restrict to this node and its descendants")
    listing.add_argument("--json", dest="as_json", action="store_true",
                         help="print the folded list as JSON")


@cli.subcommand("node", help="Write or read the front's tree.")
def _node_entry(args: argparse.Namespace) -> int:
    if args.node_verb == "add":
        return node_add_main(
            args.front, args.parent, args.kind, args.title, args.verify,
            args.must_not_touch, args.reason, args.break_, args.repo,
            what=args.what, property=args.property, scope=args.scope,
            role=args.role, after=args.after, source=args.source,
            mechanical=args.mechanical, node_id=args.node_id)
    if args.node_verb == "revise":
        return node_revise_main(
            args.front, args.id, args.reason, parent=args.parent,
            kind=args.kind, title=args.title, verify=args.verify,
            must_not_touch=args.must_not_touch, break_=args.break_,
            repo=args.repo, what=args.what, property=args.property,
            scope=args.scope, role=args.role, after=args.after,
            source=args.source, mechanical=args.mechanical,
            state=args.state)
    if args.node_verb == "list":
        return node_list_main(args.front, under=args.under,
                              as_json=args.as_json)
    raise AssertionError(f"unknown node verb {args.node_verb!r}")


_node_entry.add_arguments = add_node_arguments  # type: ignore[attr-defined]
