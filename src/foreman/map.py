"""`foreman map add|resolve|show`: the map held by the runtime.

``map add`` appends one fact to ``fronts/<name>/map.jsonl``. Only the
front's own supervisor (or the owner at a terminal) may write. ``map
resolve`` records a branch's remote sha as a seen fact. ``map show``
folds the ledger, grouped by repo then section in first-appearance
order; the foreman, the front's supervisor and the owner may read it.
"""

from __future__ import annotations

import argparse
import json

from . import caller, cli, entities, fronts, ids, paths, store
from .caller import FOREMAN, SUPERVISOR, Refusal


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


def _repo_url(record: dict, name: str) -> str:
    repos = record.get("repositories")
    repos = repos if isinstance(repos, list) else []
    for item in repos:
        if isinstance(item, dict) and str(item.get("name") or "") == name:
            return str(item.get("url") or "").strip()
    return ""


def _unknown_repo_violation(record: dict, front: str, repo: str) -> str | None:
    if record.get("shape") != "v5":
        return None
    known = _repo_names(record)
    if repo in known:
        return None
    listed = ", ".join(known) if known else "(none)"
    return (f"unknown repository '{repo}' on front "
            f"'{front}' (known: {listed})")


def _commit_fact(*, front: str, repo: str, section: str, text: str,
                 basis: str, refs: list, seen_at: str, seen_where: str,
                 commit: str, derived_from: list[str], by: str,
                 fact_id: str | None = None) -> str:
    fid = fact_id or ids.mint("map")
    now = store.utcnow_iso()
    store.append_ledger(
        paths.front_map_path(front),
        entities.MapFact(
            id=fid,
            front=front,
            repo=repo,
            section=section,
            text=text,
            basis=basis,
            refs=list(refs),
            seen_at=seen_at,
            seen_where=seen_where,
            commit=commit,
            derived_from=list(derived_from),
            at=now,
            by=by,
        ).to_dict(),
        session_id=by,
    )
    return fid


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
        unknown = _unknown_repo_violation(record, front_name, repo_name)
        if unknown:
            violations.append(unknown)
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
    fid = _commit_fact(
        front=front_name, repo=repo_name, section=section_text,
        text=fact_text, basis=basis, refs=[], seen_at=now,
        seen_where=where_text, commit=sha, derived_from=sources,
        by=who)
    print(fid)
    if weakened_by:
        if len(weakened_by) == 1:
            print(f"assumed because {weakened_by[0]} is assumed")
        else:
            print(f"assumed because {', '.join(weakened_by)} are assumed")
    return 0


def map_resolve_main(front: str, repo: str | None, ref: str | None) -> int:
    verb = "map resolve"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    if record is not None and record.get("shape") != "v5":
        violations.append(f"front '{front_name}' is not v5-shape")
    repo_name = (repo or "").strip()
    if not repo_name:
        violations.append("field '--repo' is required")
    elif record is not None:
        unknown = _unknown_repo_violation(record, front_name, repo_name)
        if unknown:
            violations.append(unknown)
    branch = "" if ref is None else str(ref).strip()
    if not branch:
        violations.append("field '--ref' is required")
    url = ""
    sha = None
    if (not violations and record is not None and repo_name and branch
            and record.get("shape") == "v5"):
        url = _repo_url(record, repo_name)
        found, err = fronts._ls_remote(url, branch)
        if err is not None or found is None:
            violations.append(f"ref '{branch}' is absent on '{url}'")
        else:
            sha = found
    if violations:
        return _refuse(violations)
    assert record is not None and sha is not None
    who = caller.by_line(me)
    now = store.utcnow_iso()
    _commit_fact(
        front=front_name, repo=repo_name, section="refs",
        text=f"origin/{branch} = {sha}", basis="seen",
        refs=[{"ref": f"origin/{branch}", "sha": sha}],
        seen_at=now, seen_where="git ls-remote", commit=sha,
        derived_from=[], by=who)
    print(f"{repo_name} {branch} {sha}")
    return 0


def _sha7(sha: object) -> str:
    return str(sha or "")[:7]


def _format_fact(fact: dict) -> str:
    basis = fact.get("basis") or ""
    text = fact.get("text") or ""
    line = f"- [{basis}] {text}"
    refs = fact.get("refs") if isinstance(fact.get("refs"), list) else []
    bits: list[str] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        bits.append(f"(ref {ref.get('ref') or ''} = {_sha7(ref.get('sha'))})")
    if bits:
        line = f"{line} {' '.join(bits)}"
    return line


def _grouped(facts: list[dict]
             ) -> list[tuple[str, list[tuple[str, list[dict]]]]]:
    repo_order: list[str] = []
    sections_for: dict[str, list[str]] = {}
    facts_for: dict[tuple[str, str], list[dict]] = {}
    for fact in facts:
        repo = str(fact.get("repo") or "")
        section = str(fact.get("section") or "")
        if repo not in sections_for:
            repo_order.append(repo)
            sections_for[repo] = []
        if section not in sections_for[repo]:
            sections_for[repo].append(section)
        facts_for.setdefault((repo, section), []).append(fact)
    return [
        (repo, [(section, facts_for[(repo, section)])
                for section in sections_for[repo]])
        for repo in repo_order
    ]


def map_show_main(front: str, repo: str | None = None,
                  as_json: bool = False) -> int:
    verb = "map show"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    if me is not None and me.role == SUPERVISOR:
        caller.check_front_supervisor(me, front_name or None, verb,
                                      violations=violations)
    else:
        caller.check_role(me, verb, FOREMAN, violations=violations)
    repo_name = (repo or "").strip()
    if violations:
        return _refuse(violations)
    facts, _by_id = _read_facts(front_name)
    if repo_name:
        facts = [fact for fact in facts if fact.get("repo") == repo_name]
    if as_json:
        print(json.dumps(facts))
        return 0
    if not facts:
        print("(no map yet)")
        return 0
    chunks: list[str] = []
    for repo_title, sections in _grouped(facts):
        lines = [repo_title]
        for section, group in sections:
            lines.append(section)
            lines.extend(_format_fact(fact) for fact in group)
        chunks.append("\n".join(lines))
    print("\n\n".join(chunks))
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
    resolve = verbs.add_parser(
        "resolve", help="Record a ref's remote sha on the map.")
    resolve.add_argument("front", help="front the fact belongs to")
    resolve.add_argument("--repo", default=None,
                         help="repository name (required)")
    resolve.add_argument("--ref", default=None,
                         help="branch to resolve on the remote (required)")
    show = verbs.add_parser("show", help="Print the front's folded map.")
    show.add_argument("front", help="front whose map to print")
    show.add_argument("--repo", default=None,
                      help="restrict to this repository")
    show.add_argument("--json", dest="as_json", action="store_true",
                      help="print the folded list as JSON")


@cli.subcommand("map", help="Write or read the front's map.")
def _map_entry(args: argparse.Namespace) -> int:
    if args.map_verb == "add":
        return map_add_main(args.front, args.repo, args.section, args.fact,
                            seen=args.seen, assumed=args.assumed,
                            where=args.where, commit=args.commit,
                            derived_from=args.derived_from)
    if args.map_verb == "resolve":
        return map_resolve_main(args.front, args.repo, args.ref)
    if args.map_verb == "show":
        return map_show_main(args.front, repo=args.repo,
                             as_json=args.as_json)
    raise AssertionError(f"unknown map verb {args.map_verb!r}")


_map_entry.add_arguments = add_map_arguments  # type: ignore[attr-defined]
