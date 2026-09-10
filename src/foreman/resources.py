"""Shared mutable resources reserved with a count.

A ``[resources]`` table in ``foreman.toml`` names each resource and how
many jobs may hold it at once. A node claims names with ``--resource``;
a running node holds each of its resources, and a returned, failed or
cancelled job holds none. Held is derived from the tree fold, the same
shape as slot grants: there is no counter to increment.
"""

from __future__ import annotations

import argparse

from . import caller, cli, config, paths, store
from .caller import Refusal


def _refuse(violations: list[str]) -> int:
    return Refusal(violations).report()


def node_resources(node: dict) -> list[str]:
    """The resource names a node claims, unique, in listed order."""
    raw = node.get("resources") if isinstance(node, dict) else None
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        names.append(text)
    return names


def collect_names(raw: list[str] | None, violations: list[str]) -> list[str]:
    """Normalize ``--resource`` values; empty names are violations."""
    names: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        text = str(item).strip()
        if not text:
            violations.append("field '--resource' names an empty resource")
            continue
        if text in seen:
            continue
        seen.add(text)
        names.append(text)
    return names


def refuse_unknown(names: list[str], violations: list[str]) -> None:
    """An unknown name is refused naming the ``[resources]`` table."""
    known = config.load().resource_names()
    known_set = set(known)
    listed = ", ".join(known) if known else "(none)"
    for name in names:
        if name not in known_set:
            violations.append(
                f"unknown resource '{name}' (not in the [resources] table; "
                f"configured: {listed})")


def _front_names() -> list[str]:
    try:
        return sorted(entry.name for entry in paths.fronts_dir().iterdir()
                      if entry.is_dir())
    except OSError:
        return []


def _running_claims() -> list[tuple[str, str, list[str]]]:
    """(front, node id, resources) for every running node that claims any."""
    claims: list[tuple[str, str, list[str]]] = []
    for name in _front_names():
        try:
            nodes = store.fold_by_id(
                store.read_ledger(paths.front_tree_path(name)))
        except OSError:
            continue
        for node in nodes:
            if str(node.get("state") or "") != "running":
                continue
            nid = node.get("id")
            if not isinstance(nid, str) or not nid:
                continue
            claimed = node_resources(node)
            if claimed:
                claims.append((name, nid, claimed))
    return claims


def held_by_name() -> dict[str, int]:
    """How many running nodes hold each resource. Derived, never stored."""
    held: dict[str, int] = {}
    for _front, _nid, names in _running_claims():
        for name in names:
            held[name] = held.get(name, 0) + 1
    return held


def holders_of(name: str) -> list[tuple[str, str]]:
    """(front, node id) pairs holding ``name``, in front then id order."""
    rows = [(front, nid) for front, nid, names in _running_claims()
            if name in names]
    rows.sort()
    return rows


def format_line(name: str, held: int, count: int) -> str:
    return f"{name}: held {held} / count {count}"


def status_lines() -> list[str]:
    """Indented resource rows for the Capacity block, or empty."""
    settings = config.load()
    names = settings.resource_names()
    if not names:
        return []
    held = held_by_name()
    lines: list[str] = []
    for name in names:
        count = settings.resource_count(name)
        shown = count if count is not None else 0
        lines.append(f"  {format_line(name, held.get(name, 0), shown)}")
    return lines


def resource_list_main() -> int:
    verb = "resource list"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, caller.FOREMAN, caller.SUPERVISOR,
                      caller.MERGE_DESK, violations=violations)
    if violations:
        return _refuse(violations)
    settings = config.load()
    names = settings.resource_names()
    held = held_by_name()
    lines: list[str] = []
    for name in names:
        count = settings.resource_count(name)
        shown = count if count is not None else 0
        lines.append(format_line(name, held.get(name, 0), shown))
        for front, nid in holders_of(name):
            lines.append(f"  {front} {nid}")
    if lines:
        print("\n".join(lines))
    return 0


def add_resource_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="resource_verb", required=True)
    verbs.add_parser("list", help="Print each resource's held/count and holders.")


@cli.subcommand("resource", help="List shared resources and who holds them.")
def _resource_entry(args: argparse.Namespace) -> int:
    if args.resource_verb == "list":
        return resource_list_main()
    raise AssertionError(f"unknown resource verb {args.resource_verb!r}")


_resource_entry.add_arguments = add_resource_arguments  # type: ignore[attr-defined]
