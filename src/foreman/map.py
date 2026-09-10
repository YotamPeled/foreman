"""`foreman map add|show`: the map held by the runtime.

``map add`` appends one fact to ``fronts/<name>/map.jsonl``. Only the
front's own supervisor (or the owner at a terminal) may write. ``map
show`` folds the ledger back; that lands in a later commit on this
module.
"""

from __future__ import annotations

import argparse

from . import caller, cli, entities, fronts, ids, paths, store
from .caller import Refusal


def _refuse(violations: list[str]) -> int:
    return Refusal(violations).report()


def _read_facts(front: str) -> tuple[list[dict], dict[str, dict]]:
    folded = store.fold_by_id(store.read_ledger(paths.front_map_path(front)))
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


def _default_commit(record: dict, repo: str) -> str:
    repos = record.get("repositories")
    repos = repos if isinstance(repos, list) else []
    for item in repos:
        if isinstance(item, dict) and str(item.get("name") or "") == repo:
            sha = item.get("base_sha")
            if sha:
                return str(sha)
    sha = record.get("base_sha")
    return str(sha) if sha else ""


def map_add_main(front: str, repo: str | None, section: str | None,
                 fact: str | None, seen: bool, assumed: bool,
                 where: str | None = None, commit: str | None = None,
                 derived_from: list[str] | None = None) -> int:
    verb = "map add"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    repo_name = (repo or "").strip()
    if not repo_name:
        violations.append("field '--repo' is required")
    elif record is not None:
        if record.get("shape") == "v5":
            known = _repo_names(record)
            if repo_name not in known:
                listed = ", ".join(known) if known else "(none)"
                violations.append(
                    f"unknown repository '{repo_name}' on front "
                    f"'{front_name}' (known: {listed})")
    section_text = "" if section is None else str(section).strip()
    if not section_text:
        violations.append("field '--section' is required")
    fact_text = "" if fact is None else str(fact).strip()
    if not fact_text:
        violations.append("field '--fact' is required")
    if seen and assumed:
        violations.append("pass exactly one of '--seen' or '--assumed'")
    elif not seen and not assumed:
        violations.append("pass exactly one of '--seen' or '--assumed'")
    where_text = "" if where is None else str(where).strip()
    if seen and not where_text:
        violations.append("field '--where' is required with '--seen'")
    sources = [str(item).strip() for item in (derived_from or [])]
    sources = [item for item in sources if item]
    for raw in derived_from or []:
        if not str(raw).strip():
            violations.append("field '--from' names an empty fact id")
    weakened_by: list[str] = []
    if record is not None:
        _facts, by_id = _read_facts(front_name)
        for src_id in sources:
            src = by_id.get(src_id)
            if src is None:
                violations.append(f"unknown fact '{src_id}'")
            elif src.get("basis") == "assumed":
                weakened_by.append(src_id)
    if violations:
        return _refuse(violations)
    assert record is not None
    basis = "assumed" if (assumed or weakened_by) else "seen"
    who = caller.by_line(me)
    now = store.utcnow_iso()
    sha = (commit if commit is not None else None)
    if sha is None:
        sha = _default_commit(record, repo_name)
    else:
        sha = str(sha).strip()
    fid = ids.mint("map")
    store.append_ledger(
        paths.front_map_path(front_name),
        entities.MapFact(
            id=fid,
            front=front_name,
            repo=repo_name,
            section=section_text,
            text=fact_text,
            basis=basis,
            refs=[],
            seen_at=now,
            seen_where=where_text,
            commit=sha,
            derived_from=sources,
            at=now,
            by=who,
        ).to_dict(),
        session_id=who,
    )
    print(fid)
    if weakened_by:
        if len(weakened_by) == 1:
            print(f"assumed because {weakened_by[0]} is assumed")
        else:
            print(f"assumed because {', '.join(weakened_by)} are assumed")
    return 0


def add_map_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="map_verb", required=True)
    add = verbs.add_parser("add", help="Append a fact to the front's map.")
    add.add_argument("front", help="front the fact belongs to")
    add.add_argument("--repo", default=None,
                     help="repository name (required)")
    add.add_argument("--section", default=None,
                     help="section heading (required)")
    add.add_argument("--fact", default=None,
                     help="the fact text (required)")
    add.add_argument("--seen", action="store_true",
                     help="the fact was observed")
    add.add_argument("--assumed", action="store_true",
                     help="the fact is inferred")
    add.add_argument("--where", default=None,
                     help="command or file the fact came from "
                          "(required with --seen)")
    add.add_argument("--commit", default=None,
                     help="repository commit the fact was read at "
                          "(default: the repository's base_sha)")
    add.add_argument("--from", dest="derived_from", action="append",
                     default=None,
                     help="fact id this one is derived from (repeatable)")


@cli.subcommand("map", help="Write or read the front's map.")
def _map_entry(args: argparse.Namespace) -> int:
    if args.map_verb == "add":
        return map_add_main(args.front, args.repo, args.section, args.fact,
                            seen=args.seen, assumed=args.assumed,
                            where=args.where, commit=args.commit,
                            derived_from=args.derived_from)
    raise AssertionError(f"unknown map verb {args.map_verb!r}")


_map_entry.add_arguments = add_map_arguments  # type: ignore[attr-defined]
