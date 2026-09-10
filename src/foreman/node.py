"""`foreman node add|revise|list|prove`: the tree's one write door.

``node add`` appends one node to ``fronts/<name>/tree.jsonl``. Only the
front's own supervisor (or the owner at a terminal) may write. The door
refuses every violation at once and names each rejected field.
``node revise`` appends a revised copy with ``op = revise``. The ledger
is append-only and folded last-wins on read. ``node list`` prints the
fold indented by depth. ``node prove`` runs the node's verify on
``--base`` then ``--head`` and records what each saw; on a proven node
it applies ``break_patch`` if one is stored.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import caller, cli, entities, fronts, ids, paths, resources, store
from .caller import FOREMAN, SUPERVISOR, Refusal
from .entities import (
    CHILDLESS_NODE_KINDS, NODE_KINDS, NODE_SCOPES, NODE_STATES,
)
from .pools._common import timeout_seconds

MAX_DEPTH = 3


def _refuse(violations: list[str]) -> int:
    return Refusal(violations).report()


def _read_nodes(front: str) -> tuple[list[dict], dict[str, dict]]:
    folded = store.fold_by_id(store.read_ledger(paths.front_tree_path(front)))
    return folded, {record["id"]: record for record in folded
                    if isinstance(record.get("id"), str)}


def tree_ever_queued(front: str) -> bool:
    """True when this front's tree has any node that was ever queued.

    The raw ledger, not the fold: a cancelled job still counts, so a
    front that has used the queue cannot go back to hand launches.
    """
    try:
        lines = store.read_ledger(paths.front_tree_path(front))
    except OSError:
        return False
    return any(
        isinstance(line, dict)
        and (line.get("state") == "queued" or line.get("queued_at"))
        for line in lines)


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


def _read_break_patch(path: str | None) -> tuple[str, str | None]:
    """File contents of ``--break-patch``, or an empty string when omitted."""
    if path is None:
        return "", None
    text = str(path).strip()
    if not text:
        return "", "field '--break-patch' is required"
    try:
        return Path(text).read_text(encoding="utf-8"), None
    except OSError:
        return "", f"cannot read --break-patch {text!r}"


def node_add_main(
        front: str, parent: str | None, kind: str | None,
        title: str | None, verify: str | None,
        must_not_touch: str | None, reason: str | None,
        break_: str | None, repo: str | None,
        what: str | None = None, property: str | None = None,
        scope: str | None = None, role: str | None = None,
        after: list[str] | None = None, source: str | None = None,
        mechanical: bool = False, node_id: str | None = None,
        break_patch: str | None = None,
        node_resources: list[str] | None = None,
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
    patch_text, patch_err = _read_break_patch(break_patch)
    if patch_err:
        violations.append(patch_err)
    claimed = resources.collect_names(node_resources, violations)
    resources.refuse_unknown(claimed, violations)
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
            break_patch=patch_text,
            resources=list(claimed),
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
        sheet_replace: str | None = None,
        sheet_reason: str | None = None,
        node_resources: list[str] | None = None,
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
    state_text = None if state is None else str(state).strip()
    if state is not None and state_text not in NODE_STATES:
        violations.append(
            "field '--state' must be queued, running, returned, "
            "verified, landed, failed, cancelled, redesign or empty")
    replace_text = None if sheet_replace is None else str(sheet_replace)
    reason_sheet = None if sheet_reason is None else str(sheet_reason).strip()
    if replace_text is not None and str(replace_text).strip():
        if not reason_sheet:
            violations.append("field '--sheet-reason' is required")
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
    claimed = None
    if node_resources is not None:
        claimed = resources.collect_names(node_resources, violations)
        resources.refuse_unknown(claimed, violations)
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
    if state_text is not None:
        updated["state"] = state_text
        if (state_text == "queued"
                and str(existing.get("state") or "") == "redesign"):
            # A redesign is cleared by the supervisor; old rounds do not
            # re-trigger. New reviews start a fresh budget.
            updated["review_rounds"] = []
    if replace_text is not None:
        updated["sheet_replace"] = replace_text
    if reason_sheet is not None:
        updated["sheet_reason"] = reason_sheet
    if claimed is not None:
        updated["resources"] = list(claimed)
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
    if (str(existing.get("kind") or "") == "milestone"
            and state_text == "landed"):
        fronts.record_working_team(front_name, who=who)
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
    caller.check_role(me, verb, FOREMAN, SUPERVISOR, violations=violations)
    caller.check_visible_front(me, front_name or None, violations=violations)
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


def _git_at(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _next_copy_n(directory: Path) -> int:
    try:
        names = [entry.name for entry in directory.iterdir()]
    except OSError:
        return 1
    highest = 0
    for name in names:
        prefix, sep, _rest = name.partition("-")
        if sep and prefix.isdigit():
            highest = max(highest, int(prefix))
    return highest + 1


def _write_prove_log(dest_dir: Path, label: str, body: str) -> str:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{_next_copy_n(dest_dir)}-{label}.log"
    dest.write_text(body or "", encoding="utf-8")
    return str(dest.resolve())


def _resolve_program(command: str) -> str | None:
    """The executable the verify would run, or None when it is not found."""
    text = command.strip()
    if not text:
        return None
    prog = text.split()[0]
    if os.path.isabs(prog) or "/" in prog:
        if os.path.isfile(prog) and os.access(prog, os.X_OK):
            return prog
        return None
    return shutil.which(prog)


def _classify_saw(exit_code: int | None, output: str, *,
                  resolved: bool, timed_out: bool) -> str:
    if timed_out:
        return "died"
    if not resolved:
        return "silent"
    if exit_code != 0:
        return "red"
    if not (output or "").strip():
        return "silent"
    return "green"


def _prove_env(command: str) -> dict[str, str]:
    child_env = os.environ.copy()
    for name in (caller.SESSION_ENV, paths.STATE_ENV, paths.CONFIG_ENV):
        child_env.pop(name, None)
    if command.strip().startswith("python"):
        child_env["PYTHONPATH"] = "src"
    return child_env


def _close_worktree(repo: str, area: str) -> None:
    _git_at(repo, "worktree", "remove", "--force", area)
    paths.remove_scratch(area)


def _execute_in(area: str, command: str, timeout_s: int
                ) -> tuple[int | None, str, bool]:
    """Run ``command`` in ``area``. Returns exit, output, timed_out."""
    try:
        proc = subprocess.run(
            command, shell=True, cwd=area,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=_prove_env(command), timeout=timeout_s)
        output_body = proc.stdout or ""
        exit_code = proc.returncode if proc.returncode is not None else 1
        return exit_code, output_body, False
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
        if isinstance(raw, bytes):
            output_body = raw.decode("utf-8", errors="replace")
        else:
            output_body = raw
        return None, output_body, True


def _open_worktree(repo: str, ref: str, key: str) -> tuple[str | None, str]:
    area = str(paths.scratch_worktree_dir("prove", key))
    added = _git_at(repo, "worktree", "add", "--detach", area, ref)
    if added.returncode != 0:
        tail = (added.stdout or "").strip()
        paths.remove_scratch(area)
        return None, (
            f"cannot open a prove worktree for '{ref}': {tail}".strip())
    return area, ""


def _run_verify_at(repo: str, ref: str, command: str, dest_dir: Path,
                   timeout_s: int, key: str, label: str
                   ) -> tuple[dict | None, str]:
    """Run ``command`` in a detached worktree of ``ref``.

    Returns ``(record, error)``. ``error`` is a refusal sentence when the
    worktree cannot be opened. The worktree is gone when this returns.
    """
    resolved = _resolve_program(command)
    if resolved is None:
        body = "resolve failed\n"
        dest = _write_prove_log(dest_dir, label, body)
        return ({
            "exit": None,
            "seconds": 0,
            "output_ref": dest,
            "saw": "silent",
        }, "")
    area, err = _open_worktree(repo, ref, key)
    if err:
        return None, err
    assert area is not None
    started = time.monotonic()
    try:
        exit_code, output_body, timed_out = _execute_in(
            area, command, timeout_s)
    finally:
        _close_worktree(repo, area)
    seconds = int(round(time.monotonic() - started))
    dest = _write_prove_log(dest_dir, label, output_body)
    return ({
        "exit": exit_code,
        "seconds": seconds,
        "output_ref": dest,
        "saw": _classify_saw(
            exit_code, output_body, resolved=True, timed_out=timed_out),
    }, "")


SURVIVED_LINE = (
    "BREAK SURVIVED: classify with node revise "
    "--break-class gap|ineffective|vacuous --reason"
)


def _run_break_at(repo: str, ref: str, command: str, dest_dir: Path,
                  timeout_s: int, key: str, patch: str
                  ) -> tuple[dict | None, str]:
    """Apply ``patch`` on a head worktree, run verify, restore.

    Returns ``({saw, verdict}, error)``. The worktree is gone afterwards.
    """
    area, err = _open_worktree(repo, ref, key)
    if err:
        return None, err
    assert area is not None
    started = time.monotonic()
    try:
        applied = subprocess.run(
            ["git", "-C", area, "apply", "-"],
            input=patch, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        if applied.returncode != 0:
            tail = (applied.stdout or "").strip()
            return None, f"cannot apply break_patch: {tail}".strip()
        exit_code, output_body, timed_out = _execute_in(
            area, command, timeout_s)
        _git_at(area, "checkout", "--", ".")
        _git_at(area, "clean", "-fd")
    finally:
        _close_worktree(repo, area)
    seconds = int(round(time.monotonic() - started))
    dest = _write_prove_log(dest_dir, "break", output_body)
    saw = _classify_saw(
        exit_code, output_body, resolved=True, timed_out=timed_out)
    verdict = "red" if exit_code not in (0, None) else "survived"
    if timed_out:
        verdict = "survived"
    return ({
        "saw": saw,
        "verdict": verdict,
        "exit": exit_code,
        "seconds": seconds,
        "output_ref": dest,
    }, "")


def _append_prove(existing: dict, front_name: str, prove: dict,
                  who: str, now: str) -> None:
    updated = dict(existing)
    updated["prove"] = prove
    updated["op"] = "revise"
    updated["reason_revised"] = "prove"
    updated["at"] = now
    updated["by"] = who
    line = entities.Node.from_dict(updated).to_dict()
    line["op"] = "revise"
    line["reason_revised"] = "prove"
    line["at"] = now
    line["by"] = who
    line["prove"] = prove
    store.append_ledger(
        paths.front_tree_path(front_name), line, session_id=who)


def node_prove_main(front: str, node_id: str | None,
                    base: str | None, head: str | None,
                    repo: str | None = None,
                    timeout: str | None = None) -> int:
    verb = "node prove"
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
    base_ref = _required("--base", base, violations)
    head_ref = _required("--head", head, violations)
    repo_path = _required("--repo", repo, violations)
    timeout_text = "30m" if timeout is None else str(timeout).strip()
    if not timeout_text:
        timeout_text = "30m"
    timeout_s = timeout_seconds(timeout_text)
    if timeout_s is None:
        violations.append("field '--timeout' is not a duration")
    existing: dict | None = None
    if record is not None and nid:
        _folded, by_id = _read_nodes(front_name)
        existing = by_id.get(nid)
        if existing is None:
            violations.append(f"unknown node '{nid}'")
        elif not str(existing.get("verify") or "").strip():
            violations.append(f"node '{nid}' has no verify")
    if repo_path and not violations:
        listed = _git_at(repo_path, "rev-parse", "--is-inside-work-tree")
        if listed.returncode != 0:
            violations.append(
                f"--repo '{repo_path}' is not a git repository")
        else:
            repo_path = str(Path(repo_path).resolve())
            for flag, ref in (("--base", base_ref), ("--head", head_ref)):
                if not ref:
                    continue
                checked = _git_at(
                    repo_path, "rev-parse", "--verify", ref)
                if checked.returncode != 0:
                    violations.append(
                        f"{flag} '{ref}' does not resolve")
    if violations:
        return _refuse(violations)
    assert record is not None and existing is not None
    assert timeout_s is not None
    who = caller.by_line(me)
    now = store.utcnow_iso()
    command = str(existing.get("verify") or "").strip()
    dest_dir = paths.session_dir(who) / "prove"
    base_run, err = _run_verify_at(
        repo_path, base_ref, command, dest_dir, timeout_s,
        f"{nid}-base", "base")
    if err:
        return _refuse([err])
    assert base_run is not None
    head_run, err = _run_verify_at(
        repo_path, head_ref, command, dest_dir, timeout_s,
        f"{nid}-head", "head")
    if err:
        return _refuse([err])
    assert head_run is not None
    if base_run["saw"] == "red" and head_run["saw"] == "green":
        verdict = "proven"
    else:
        verdict = (f"unproven (base: {base_run['saw']}, "
                   f"head: {head_run['saw']})")
    prove = {
        "base": base_run,
        "head": head_run,
        "verdict": verdict,
        "at": now,
    }
    exit_code = 0 if verdict == "proven" else 1
    if verdict == "proven":
        patch = str(existing.get("break_patch") or "")
        if not patch:
            prove["break"] = {"verdict": "prose only"}
            print(f"{nid} {verdict}")
            print(
                "warning: no break_patch; until the patch exists "
                "the supervisor applies the prose by hand",
                file=sys.stderr)
            _append_prove(existing, front_name, prove, who, now)
            return 0
        broke, err = _run_break_at(
            repo_path, head_ref, command, dest_dir, timeout_s,
            f"{nid}-break", patch)
        if err:
            return _refuse([err])
        assert broke is not None
        prove["break"] = {
            "saw": broke["saw"],
            "verdict": broke["verdict"],
        }
        _append_prove(existing, front_name, prove, who, now)
        print(f"{nid} {verdict}")
        if broke["verdict"] == "survived":
            print(SURVIVED_LINE)
            return 3
        return 0
    _append_prove(existing, front_name, prove, who, now)
    print(f"{nid} {verdict}")
    return exit_code


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
    add.add_argument("--break-patch", dest="break_patch", default=None,
                     help="unified diff that must make verify go red")
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
    add.add_argument("--resource", dest="resources", action="append",
                     default=None,
                     help="shared resource this node holds while running "
                          "(repeatable)")
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
                        help="queued, running, returned, verified, "
                             "landed, failed, cancelled, redesign or empty")
    revise.add_argument("--sheet-replace", dest="sheet_replace",
                        default=None,
                        help="replace the role's default sheet "
                             "(requires --sheet-reason)")
    revise.add_argument("--sheet-reason", dest="sheet_reason",
                        default=None,
                        help="why the default sheet is replaced "
                             "(required with --sheet-replace)")
    revise.add_argument("--resource", dest="resources", action="append",
                        default=None,
                        help="replace the resources list (repeatable)")
    listing = verbs.add_parser(
        "list", help="Print the front's folded tree.")
    listing.add_argument("front", help="front whose tree to print")
    listing.add_argument("--under", default=None,
                         help="restrict to this node and its descendants")
    listing.add_argument("--json", dest="as_json", action="store_true",
                         help="print the folded list as JSON")
    prove = verbs.add_parser(
        "prove", help="Run the node's verify on --base then --head.")
    prove.add_argument("front", help="front the node belongs to")
    prove.add_argument("id", help="node to prove")
    prove.add_argument("--base", default=None,
                       help="git ref that must go red (required)")
    prove.add_argument("--head", default=None,
                       help="git ref that must go green (required)")
    prove.add_argument("--repo", default=None,
                       help="local clone to open scratch worktrees in "
                            "(required)")
    prove.add_argument("--timeout", default="30m",
                       help="how long each verify may run (default: 30m)")


@cli.subcommand("node", help="Write or read the front's tree.")
def _node_entry(args: argparse.Namespace) -> int:
    if args.node_verb == "add":
        return node_add_main(
            args.front, args.parent, args.kind, args.title, args.verify,
            args.must_not_touch, args.reason, args.break_, args.repo,
            what=args.what, property=args.property, scope=args.scope,
            role=args.role, after=args.after, source=args.source,
            mechanical=args.mechanical, node_id=args.node_id,
            break_patch=args.break_patch, node_resources=args.resources)
    if args.node_verb == "revise":
        return node_revise_main(
            args.front, args.id, args.reason, parent=args.parent,
            kind=args.kind, title=args.title, verify=args.verify,
            must_not_touch=args.must_not_touch, break_=args.break_,
            repo=args.repo, what=args.what, property=args.property,
            scope=args.scope, role=args.role, after=args.after,
            source=args.source, mechanical=args.mechanical,
            state=args.state, sheet_replace=args.sheet_replace,
            sheet_reason=args.sheet_reason, node_resources=args.resources)
    if args.node_verb == "list":
        return node_list_main(args.front, under=args.under,
                              as_json=args.as_json)
    if args.node_verb == "prove":
        return node_prove_main(
            args.front, args.id, args.base, args.head,
            repo=args.repo, timeout=args.timeout)
    raise AssertionError(f"unknown node verb {args.node_verb!r}")


_node_entry.add_arguments = add_node_arguments  # type: ignore[attr-defined]
