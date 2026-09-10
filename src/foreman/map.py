"""`foreman map add|resolve|import|show`: the map held by the runtime.

``map add`` appends one fact to ``fronts/<name>/map.jsonl``. Only the
front's own supervisor (or the owner at a terminal) may write. ``map
resolve`` records a branch's remote sha as a seen fact. ``map import``
reads a map.md through the same add path. ``map show`` folds the ledger,
grouped by repo then section in first-appearance order; the foreman, the
front's supervisor and the owner may read it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from . import caller, cli, entities, fronts, ids, paths, store
from .caller import FOREMAN, SUPERVISOR, Refusal

_REPO_HEAD = re.compile(r"^## Repository:\s+(\S+)")
_SECTION_HEAD = re.compile(r"^###\s+(.+)$")
_TAG = re.compile(r"\*\*\[(seen|assumed)\]\*\*")
_WHERE = re.compile(r"\(`([^`]+)`\)")


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


def _add_field_violations(
        record: dict | None, front_name: str, repo_name: str,
        section_text: str, fact_text: str, seen: bool, assumed: bool,
        where_text: str, derived_from: list[str] | None,
        by_id: dict[str, dict] | None) -> tuple[list[str], list[str]]:
    """The field refusals ``map add`` names, shared with ``map import``."""
    violations: list[str] = []
    if not repo_name:
        violations.append("field '--repo' is required")
    elif record is not None:
        unknown = _unknown_repo_violation(record, front_name, repo_name)
        if unknown:
            violations.append(unknown)
    if not section_text:
        violations.append("field '--section' is required")
    if not fact_text:
        violations.append("field '--fact' is required")
    if seen and assumed:
        violations.append("pass exactly one of '--seen' or '--assumed'")
    elif not seen and not assumed:
        violations.append("pass exactly one of '--seen' or '--assumed'")
    if seen and not where_text:
        violations.append("field '--where' is required with '--seen'")
    sources = [str(item).strip() for item in (derived_from or [])]
    sources = [item for item in sources if item]
    for raw in derived_from or []:
        if not str(raw).strip():
            violations.append("field '--from' names an empty fact id")
    weakened_by: list[str] = []
    if by_id is not None:
        for src_id in sources:
            src = by_id.get(src_id)
            if src is None:
                violations.append(f"unknown fact '{src_id}'")
            elif src.get("basis") == "assumed":
                weakened_by.append(src_id)
    return violations, weakened_by


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
    section_text = "" if section is None else str(section).strip()
    fact_text = "" if fact is None else str(fact).strip()
    where_text = "" if where is None else str(where).strip()
    by_id: dict[str, dict] | None = None
    if record is not None:
        _facts, by_id = _read_facts(front_name)
    field, weakened_by = _add_field_violations(
        record, front_name, repo_name, section_text, fact_text,
        seen, assumed, where_text, derived_from, by_id)
    violations.extend(field)
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
    sources = [str(item).strip() for item in (derived_from or [])]
    sources = [item for item in sources if item]
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


def _import_id(repo: str, section: str, text: str) -> str:
    digest = hashlib.sha1(
        f"{repo}\n{section}\n{text}".encode()).hexdigest()
    return "map-" + digest[:7]


def _basis_and_where(text: str, default_where: str, table: bool
                     ) -> tuple[str | None, str]:
    match = _TAG.search(text)
    if match is None:
        return ("assumed" if table else None), default_where
    after = text[match.end():]
    where_m = _WHERE.search(after)
    where = where_m.group(1).strip() if where_m else default_where
    return match.group(1), where


def _parse_map_md(text: str, default_where: str
                  ) -> tuple[list[dict], list[str]]:
    """Facts from a map.md, plus parse-level refusals (untagged lines)."""
    facts: list[dict] = []
    violations: list[str] = []
    lines = text.splitlines()
    repo: str | None = None
    section = ""
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        repo_m = _REPO_HEAD.match(stripped)
        if repo_m:
            repo = repo_m.group(1)
            section = f"Repository: {repo}"
            i += 1
            continue
        if repo is None:
            i += 1
            continue
        section_m = _SECTION_HEAD.match(stripped)
        if section_m:
            section = section_m.group(1).strip()
            i += 1
            continue
        if stripped.startswith("## "):
            i += 1
            continue
        if stripped.startswith("|"):
            start = i + 1
            fact_text = stripped
            basis, where = _basis_and_where(fact_text, default_where, True)
            facts.append({
                "line": start, "repo": repo, "section": section,
                "text": fact_text, "basis": basis, "where": where,
            })
            i += 1
            continue
        if stripped.startswith("- "):
            start = i + 1
            block = [stripped[2:].strip()]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():
                    break
                if nxt.startswith(" ") or nxt.startswith("\t"):
                    block.append(nxt.strip())
                    i += 1
                    continue
                break
            fact_text = " ".join(part for part in block if part)
            basis, where = _basis_and_where(fact_text, default_where, False)
            facts.append({
                "line": start, "repo": repo, "section": section,
                "text": fact_text, "basis": basis, "where": where,
            })
            continue
        if not stripped:
            i += 1
            continue
        start = i + 1
        block = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i]
            ns = nxt.strip()
            if not ns:
                break
            if (ns.startswith("## ") or ns.startswith("### ")
                    or ns.startswith("- ") or ns.startswith("|")
                    or _REPO_HEAD.match(ns) or _SECTION_HEAD.match(ns)):
                break
            block.append(ns)
            i += 1
        fact_text = " ".join(part for part in block if part)
        basis, where = _basis_and_where(fact_text, default_where, False)
        facts.append({
            "line": start, "repo": repo, "section": section,
            "text": fact_text, "basis": basis, "where": where,
        })
    for fact in facts:
        if fact["basis"] is None:
            violations.append(
                f"line {fact['line']} has neither **[seen]** nor "
                f"**[assumed]**")
    return facts, violations


def map_import_main(front: str, file: str | None, seen_at: str | None,
                    commit: str | None) -> int:
    verb = "map import"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    path_text = "" if file is None else str(file).strip()
    if not path_text:
        violations.append("field 'file' is required")
    seen_at_text = "" if seen_at is None else str(seen_at).strip()
    if not seen_at_text:
        violations.append("field '--seen-at' is required")
    commit_text = "" if commit is None else str(commit).strip()
    if not commit_text:
        violations.append("field '--commit' is required")
    body = None
    default_where = Path(path_text).name if path_text else ""
    if path_text:
        try:
            body = Path(path_text).read_text(encoding="utf-8")
        except OSError:
            violations.append(f"file '{path_text}' cannot be read")
    if body is not None:
        parsed, parse_violations = _parse_map_md(body, default_where)
        violations.extend(parse_violations)
        for fact in parsed:
            if fact["basis"] is None:
                continue
            field, _weak = _add_field_violations(
                record, front_name, fact["repo"], fact["section"],
                fact["text"], fact["basis"] == "seen",
                fact["basis"] == "assumed", fact["where"], None, None)
            for item in field:
                violations.append(f"line {fact['line']}: {item}")
    if violations:
        return _refuse(violations)
    assert record is not None
    who = caller.by_line(me)
    _facts, by_id = _read_facts(front_name)
    imported = 0
    already = 0
    seen_ids = set(by_id)
    for fact in parsed:
        fid = _import_id(fact["repo"], fact["section"], fact["text"])
        if fid in seen_ids:
            already += 1
            continue
        seen_ids.add(fid)
        _commit_fact(
            front=front_name, repo=fact["repo"], section=fact["section"],
            text=fact["text"], basis=fact["basis"], refs=[],
            seen_at=seen_at_text, seen_where=fact["where"],
            commit=commit_text, derived_from=[], by=who, fact_id=fid)
        imported += 1
    print(f"imported {imported} facts, {already} already present")
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
    caller.check_role(me, verb, FOREMAN, SUPERVISOR, violations=violations)
    caller.check_visible_front(me, front_name or None, violations=violations)
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
    imp = verbs.add_parser(
        "import", help="Read a map.md into the front's map.")
    imp.add_argument("front", help="front the facts belong to")
    imp.add_argument("file", help="path to a map.md")
    imp.add_argument("--seen-at", default=None,
                     help="timestamp every imported fact was observed "
                          "(required)")
    imp.add_argument("--commit", default=None,
                     help="commit every imported fact was read at "
                          "(required)")
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
    if args.map_verb == "import":
        return map_import_main(args.front, args.file, args.seen_at,
                               args.commit)
    if args.map_verb == "show":
        return map_show_main(args.front, repo=args.repo,
                             as_json=args.as_json)
    raise AssertionError(f"unknown map verb {args.map_verb!r}")


_map_entry.add_arguments = add_map_arguments  # type: ignore[attr-defined]
