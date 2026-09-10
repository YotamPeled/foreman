"""`foreman front import`: read map.md, milestones.jsonl and tree.jsonl
through the map, milestone and node doors.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from . import caller, fronts, milestone, node
from .caller import Refusal

_MILESTONE_KEYS = (
    "title", "done_when", "verify", "reason", "break", "order")
_NODE_KEYS = (
    "parent", "kind", "title", "repo", "what", "verify", "must_not_touch",
    "reason", "break", "property", "scope", "role", "after", "source",
    "mechanical", "state")
# Historical add lines predate --break. The door requires a non-empty
# value; a later op=revise line in the same file carries the real one.
_PLACEHOLDER_BREAK = "-"


def _refuse(violations: list[str]) -> int:
    return Refusal(violations).report()


def _call_door(fn, *args, **kwargs) -> tuple[int, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(*args, **kwargs)
    return code, err.getvalue()


def _stop(filename: str, lineno: int, err_text: str) -> int:
    detail = err_text.strip()
    prefix = "foreman: refused:"
    if detail.startswith(prefix):
        detail = detail[len(prefix):].strip()
    print(f"foreman: refused: {filename}:{lineno}: {detail}",
          file=sys.stderr)
    return 1


def _norm(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return [_norm(item) for item in value]
    if value is None:
        return None
    return value


def _present_value(line: dict, key: str) -> bool:
    if key not in line:
        return False
    value = line.get(key)
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _fields_match(line: dict, existing: dict, keys: tuple[str, ...]) -> bool:
    for key in keys:
        if not _present_value(line, key):
            continue
        if _norm(line.get(key)) != _norm(existing.get(key)):
            return False
    return True


def _read_jsonl(path: Path) -> tuple[list[tuple[int, dict]] | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, f"file '{path}' cannot be read"
    records: list[tuple[int, dict]] = []
    for lineno, text in enumerate(raw.splitlines(), 1):
        if not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"{path.name}:{lineno}: {exc.msg}"
        if not isinstance(parsed, dict):
            return None, f"{path.name}:{lineno}: not a JSON object"
        records.append((lineno, parsed))
    return records, None


def _break_arg(line: dict) -> str:
    text = str(line.get("break") or "").strip()
    return text or _PLACEHOLDER_BREAK


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _after_arg(line: dict) -> list[str] | None:
    raw = line.get("after")
    if not isinstance(raw, list):
        return None
    return [str(item).strip() for item in raw]


def _milestone_by_id(front: str) -> dict[str, dict]:
    _raw, folded = milestone._read(front)
    return {item["id"]: item for item in folded
            if isinstance(item.get("id"), str)}


def _node_by_id(front: str) -> dict[str, dict]:
    _folded, by_id = node._read_nodes(front)
    return by_id


def _import_milestones(front: str, records: list[tuple[int, dict]],
                       filename: str, dry_run: bool
                       ) -> tuple[int, int, int] | int:
    new = present = revised = 0
    simulated = dict(_milestone_by_id(front))
    for lineno, line in records:
        by_id = simulated if dry_run else _milestone_by_id(front)
        nid = str(line.get("id") or "").strip()
        existing = by_id.get(nid) if nid else None
        if existing is not None and _fields_match(line, existing, _MILESTONE_KEYS):
            present += 1
            continue
        if dry_run:
            if existing is not None:
                revised += 1
                simulated[nid] = {**existing, **line}
            else:
                new += 1
                if nid:
                    simulated[nid] = line
            continue
        if existing is not None:
            code, err = _call_door(
                milestone.milestone_revise_main, front, nid, "import",
                title=_str_or_none(line.get("title")),
                done_when=_str_or_none(line.get("done_when")),
                verify=_str_or_none(line.get("verify")),
                break_=_str_or_none(line.get("break")) if _present_value(line, "break") else None,
                order=_int_or_none(line.get("order")))
            if code != 0:
                return _stop(filename, lineno, err)
            if err:
                sys.stderr.write(err)
            revised += 1
            continue
        code, err = _call_door(
            milestone.milestone_add_main, front,
            _str_or_none(line.get("title")),
            _str_or_none(line.get("done_when")),
            _str_or_none(line.get("verify")),
            _str_or_none(line.get("reason")),
            _break_arg(line),
            order=_int_or_none(line.get("order")),
            milestone_id=nid or None)
        if code != 0:
            return _stop(filename, lineno, err)
        if err:
            sys.stderr.write(err)
        new += 1
        if nid:
            simulated[nid] = line
    return new, present, revised


def _node_add_kwargs(front: str, line: dict) -> dict:
    after = _after_arg(line)
    mechanical = bool(line.get("mechanical")) if "mechanical" in line else False
    return dict(
        front=front,
        parent=_str_or_none(line.get("parent")),
        kind=_str_or_none(line.get("kind")),
        title=_str_or_none(line.get("title")),
        verify=_str_or_none(line.get("verify")),
        must_not_touch=_str_or_none(line.get("must_not_touch")),
        reason=_str_or_none(line.get("reason")),
        break_=_break_arg(line),
        repo=_str_or_none(line.get("repo")),
        what=_str_or_none(line.get("what")) if "what" in line else None,
        property=_str_or_none(line.get("property")) if "property" in line else None,
        scope=_str_or_none(line.get("scope")) if _present_value(line, "scope") else None,
        role=_str_or_none(line.get("role")) if _present_value(line, "role") else None,
        after=after,
        source=_str_or_none(line.get("source")) if _present_value(line, "source") else None,
        mechanical=mechanical,
        node_id=str(line.get("id") or "").strip() or None,
    )


def _node_revise_kwargs(front: str, line: dict, reason: str) -> dict:
    kwargs: dict = dict(
        front=front,
        node_id=str(line.get("id") or "").strip() or None,
        reason=reason,
    )
    if "title" in line:
        kwargs["title"] = _str_or_none(line.get("title"))
    if "verify" in line:
        kwargs["verify"] = _str_or_none(line.get("verify"))
    if "must_not_touch" in line:
        kwargs["must_not_touch"] = _str_or_none(line.get("must_not_touch"))
    if _present_value(line, "break"):
        kwargs["break_"] = _str_or_none(line.get("break"))
    if "repo" in line:
        kwargs["repo"] = _str_or_none(line.get("repo"))
    if "what" in line:
        kwargs["what"] = _str_or_none(line.get("what"))
    if "property" in line:
        kwargs["property"] = _str_or_none(line.get("property"))
    if _present_value(line, "scope"):
        kwargs["scope"] = _str_or_none(line.get("scope"))
    if "role" in line:
        kwargs["role"] = _str_or_none(line.get("role"))
    if "after" in line:
        kwargs["after"] = _after_arg(line) or []
    if "source" in line:
        kwargs["source"] = _str_or_none(line.get("source"))
    if line.get("mechanical"):
        kwargs["mechanical"] = True
    if "state" in line:
        kwargs["state"] = _str_or_none(line.get("state"))
    return kwargs


def _import_tree(front: str, records: list[tuple[int, dict]],
                 filename: str, dry_run: bool
                 ) -> tuple[int, int, int] | int:
    new = present = revised = 0
    simulated = dict(_node_by_id(front))
    for lineno, line in records:
        by_id = simulated if dry_run else _node_by_id(front)
        nid = str(line.get("id") or "").strip()
        existing = by_id.get(nid) if nid else None
        is_revise = str(line.get("op") or "") == "revise"
        if existing is not None and _fields_match(line, existing, _NODE_KEYS):
            present += 1
            continue
        if dry_run:
            if existing is not None:
                revised += 1
                simulated[nid] = {**existing, **line}
            elif is_revise:
                return _stop(
                    filename, lineno,
                    f"foreman: refused: unknown node '{nid}'")
            else:
                new += 1
                if nid:
                    simulated[nid] = line
            continue
        if existing is not None or is_revise:
            reason = str(line.get("reason_revised") or "").strip() or "import"
            code, err = _call_door(
                node.node_revise_main, **_node_revise_kwargs(front, line, reason))
            if code != 0:
                return _stop(filename, lineno, err)
            if err:
                sys.stderr.write(err)
            revised += 1
            continue
        code, err = _call_door(node.node_add_main, **_node_add_kwargs(front, line))
        if code != 0:
            return _stop(filename, lineno, err)
        if err:
            sys.stderr.write(err)
        new += 1
        if nid:
            simulated[nid] = line
    return new, present, revised


def import_milestones_and_tree(
        front: str, directory: Path, dry_run: bool = False
        ) -> tuple[dict[str, tuple[int, int, int]] | None, int]:
    """Import milestones.jsonl then tree.jsonl. None counts means a refusal."""
    counts: dict[str, tuple[int, int, int]] = {}
    mil_path = directory / "milestones.jsonl"
    if mil_path.is_file():
        records, err = _read_jsonl(mil_path)
        if err is not None:
            print(f"foreman: refused: {err}", file=sys.stderr)
            return None, 1
        assert records is not None
        result = _import_milestones(front, records, "milestones.jsonl", dry_run)
        if isinstance(result, int):
            return None, result
        counts["milestones.jsonl"] = result
    tree_path = directory / "tree.jsonl"
    if tree_path.is_file():
        records, err = _read_jsonl(tree_path)
        if err is not None:
            print(f"foreman: refused: {err}", file=sys.stderr)
            return None, 1
        assert records is not None
        result = _import_tree(front, records, "tree.jsonl", dry_run)
        if isinstance(result, int):
            return None, result
        counts["tree.jsonl"] = result
    return counts, 0


def front_import_main(front: str, directory: str | None,
                      seen_at: str | None = None,
                      commit: str | None = None,
                      dry_run: bool = False) -> int:
    verb = "front import"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    dir_text = "" if directory is None else str(directory).strip()
    if not dir_text:
        violations.append("field 'dir' is required")
    dir_path = Path(dir_text) if dir_text else None
    if dir_path is not None and not dir_path.is_dir():
        violations.append(f"directory '{dir_text}' cannot be read")
    if violations:
        return _refuse(violations)
    assert record is not None and dir_path is not None
    counts, code = import_milestones_and_tree(
        front_name, dir_path, dry_run=dry_run)
    if code != 0 or counts is None:
        return 1
    return 0
