"""`foreman front add|list|prefer|close`: a brief becomes a front on the ledger.

``front add <dir>`` reads ``<dir>/brief.toml`` with :mod:`tomllib`, refuses
with every violation named at once (docs/DESIGN.md section 4.3), and otherwise
appends one front line to ``fronts/<name>/front.jsonl`` plus one line per task
to ``fronts/<name>/tasks.jsonl``, then copies the brief (and ``plan.md`` when
the directory has one) beside the config so the front no longer depends on the
directory it came from. ``front prefer`` and ``front close`` never edit: they
append a revised copy of the front line and readers fold last-wins, exactly
like every other ledger in the state directory.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from . import caller, cli, entities, ids, paths, store
from .caller import Refusal
from .entities import JOB_ROLES

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

#: The four headings every task scope must carry (docs/DESIGN.md section 4.2).
_SCOPE_PARTS = ("WHAT", "INPUTS", "OUTPUTS", "OUT OF SCOPE")

_SENTENCE_ENDS = (".", "!", "?")

#: An alert is a comparison operator and a number: `< 0.80`, `>= 5`, `!= 0`.
_ALERT_RE = re.compile(
    r"^\s*(?:<=|>=|==|!=|<|>)\s*[+-]?"
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*$")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_one_sentence(text: str) -> bool:
    """One sentence: a single line with one sentence boundary in it.

    A boundary is a terminator followed by whitespace or the end of the line,
    so file names like ``briefs/panel/brief.toml`` do not read as three
    sentences. The line must end on its boundary.
    """
    stripped = text.strip()
    if not stripped or "\n" in stripped:
        return False
    if not stripped.endswith(_SENTENCE_ENDS):
        return False
    return len(re.findall(r"[.!?](?=\s|$)", stripped)) == 1


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """A task-after cycle as a title path, or None when the graph is clean."""
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> list[str] | None:
        visiting.append(node)
        for nxt in graph[node]:
            if nxt not in graph:
                continue
            if nxt in visiting:
                return visiting[visiting.index(nxt):] + [nxt]
            if nxt not in done:
                hit = visit(nxt)
                if hit is not None:
                    return hit
        visiting.pop()
        done.add(node)
        return None

    for node in graph:
        if node not in done:
            hit = visit(node)
            if hit is not None:
                return hit
    return None


def read_front_record(name: str) -> dict | None:
    """The folded front line for an added front; None when it has no record."""
    try:
        folded = store.fold_by_id(store.read_ledger(paths.front_record_path(name)))
    except OSError:
        return None
    return folded[-1] if folded else None


def _existing_fronts() -> set[str]:
    """Names already on the ledger: a front directory holding a front line."""
    try:
        names = [entry.name for entry in paths.fronts_dir().iterdir()
                 if entry.is_dir()]
    except OSError:
        return set()
    return {name for name in names if read_front_record(name) is not None}


def _branch_violation(directory: str, branch: str) -> str | None:
    """None when refs/heads/<branch> exists in the repo holding directory.

    One `git show-ref` at most; repo membership is a directory walk, not a
    second subprocess. A directory outside any repository, or a missing git
    binary, is its own violation rather than a silent pass.
    """
    node = Path(directory) if Path(directory).is_dir() else Path(directory).parent
    if not any((parent / ".git").exists() for parent in (node, *node.parents)):
        return (f"field 'land-on' branch '{branch}' cannot be checked: "
                f"brief directory '{directory}' is not inside a git repository")
    try:
        completed = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet",
             f"refs/heads/{branch}"],
            cwd=str(directory),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return (f"field 'land-on' branch '{branch}' cannot be checked: "
                f"git is not available")
    if completed.returncode != 0:
        return f"field 'land-on' branch '{branch}' does not exist"
    return None


def _validate(data: dict, existing: set[str],
              directory: str | None = None) -> list[str]:
    """Every 4.3 violation at once, each naming the field or rule it breaks."""
    violations: list[str] = []

    name = data.get("name")
    if name is None or (isinstance(name, str) and not name.strip()):
        violations.append("field 'name' is required (a non-empty string)")
        name = None
    elif not isinstance(name, str):
        violations.append("field 'name' must be a non-empty string")
        name = None
    elif not _NAME_RE.fullmatch(name.strip()):
        violations.append(f"field 'name' must be a directory-safe name "
                          f"(got '{name.strip()}')")
    elif name.strip() in existing:
        violations.append(f"front '{name.strip()}' is already on the ledger "
                          f"(field 'name' must be unique)")

    want = data.get("want")
    if not (isinstance(want, str) and want.strip()):
        violations.append("field 'want' is required (a non-empty string)")

    done_when = data.get("done-when")
    if done_when is None or (isinstance(done_when, str) and not done_when.strip()):
        violations.append("field 'done-when' is required (a non-empty string)")
    elif not isinstance(done_when, str) or not _is_one_sentence(done_when):
        violations.append("field 'done-when' must be one sentence")

    land_on = data.get("land-on")
    if not (isinstance(land_on, str) and land_on.strip()):
        violations.append("field 'land-on' is required (a non-empty string)")
    elif directory is not None:
        problem = _branch_violation(directory, land_on.strip())
        if problem is not None:
            violations.append(problem)

    for key in ("order", "prefer"):
        if key in data and not _is_int(data[key]):
            violations.append(f"field '{key}' must be an integer")

    after = data.get("after", [])
    if not isinstance(after, list):
        violations.append("field 'after' must be a list of front names")
        after = []
    else:
        for entry in after:
            if not (isinstance(entry, str) and entry):
                violations.append("field 'after' must be a list of front names")
            elif entry not in existing:
                violations.append(f"field 'after' names unknown front '{entry}'")

    allocation = data.get("allocation", {})
    if not isinstance(allocation, dict):
        violations.append("field 'allocation' must be a table of role to count")
        allocation = {}
    else:
        for role, count in allocation.items():
            if role not in JOB_ROLES:
                violations.append(f"field 'allocation' names unknown role '{role}'")
            elif not _is_int(count) or count < 0:
                violations.append(f"field 'allocation' for role '{role}' must be "
                                   f"a non-negative integer")

    tasks = data.get("task", [])
    if not isinstance(tasks, list):
        violations.append("field 'task' must be a list")
        tasks = []
    titles: list[str] = []
    for index, task in enumerate(tasks):
        tag = f"task #{index + 1}"
        if not isinstance(task, dict):
            violations.append(f"{tag} must be a table")
            continue
        title = task.get("title")
        label = f"task '{title}'" if isinstance(title, str) and title else tag
        if not (isinstance(title, str) and title):
            violations.append(f"{tag}: field 'title' is required")
        else:
            titles.append(title)
        scope = task.get("scope")
        if not (isinstance(scope, str) and scope):
            violations.append(f"{label}: field 'scope' is required")
        else:
            missing = [part for part in _SCOPE_PARTS if part not in scope]
            if missing:
                violations.append(
                    f"{label}: field 'scope' must contain WHAT, INPUTS, OUTPUTS "
                    f"and OUT OF SCOPE (missing: {', '.join(missing)})")
        verify = task.get("verify")
        if not (isinstance(verify, str) and verify.strip()):
            violations.append(f"{label}: field 'verify' is required")
        size = task.get("size")
        if not _is_int(size) or (isinstance(size, int) and size < 1):
            violations.append(f"{label}: field 'size' must be a positive integer")
        task_after = task.get("after", [])
        if not isinstance(task_after, list):
            violations.append(f"{label}: field 'after' must be a list of task titles")
        else:
            for entry in task_after:
                if not (isinstance(entry, str) and entry):
                    violations.append(f"{label}: field 'after' must be a list "
                                      f"of task titles")
        for key in ("timeout", "land-on"):
            if key in task and not isinstance(task[key], str):
                violations.append(f"{label}: field '{key}' must be a string")
        if "core" in task and not isinstance(task["core"], bool):
            violations.append(f"{label}: field 'core' must be true or false")
    seen: set[str] = set()
    for title in titles:
        if title in seen:
            violations.append(f"task title '{title}' is listed twice")
        seen.add(title)
    known = set(titles)
    for task in tasks:
        if not isinstance(task, dict):
            continue
        title = task.get("title")
        label = f"task '{title}'" if isinstance(title, str) and title else "task"
        task_after = task.get("after", [])
        if not isinstance(task_after, list):
            continue
        for entry in task_after:
            if isinstance(entry, str) and entry and entry not in known:
                violations.append(f"{label}: field 'after' names unknown task "
                                  f"'{entry}'")
    graph = {title: [entry for entry in _task_after(tasks, title)
                     if isinstance(entry, str) and entry in known]
             for title in titles}
    cycle = _find_cycle(graph)
    if cycle is not None:
        violations.append("task 'after' graph has a cycle: "
                          + " -> ".join(cycle))

    monitors = data.get("monitor", [])
    if not isinstance(monitors, list):
        violations.append("field 'monitor' must be a list")
    else:
        for index, monitor in enumerate(monitors):
            if not isinstance(monitor, dict):
                violations.append(f"monitor #{index + 1} must be a table")
                continue
            measure = monitor.get("measure")
            if not (isinstance(measure, str) and measure.strip()):
                violations.append(f"monitor #{index + 1}: field 'measure' must be "
                                  f"a non-empty string")
            unit = monitor.get("unit")
            if not (isinstance(unit, str) and unit.strip()):
                violations.append(f"monitor #{index + 1}: field 'unit' must be "
                                  f"a non-empty string")
            every = monitor.get("every")
            if not (isinstance(every, str) and every.strip()):
                violations.append(f"monitor #{index + 1}: field 'every' must be "
                                  f"a non-empty string")
            alert = monitor.get("alert")
            if alert is not None and not (
                    isinstance(alert, str) and _ALERT_RE.match(alert)):
                violations.append(f"monitor #{index + 1}: field 'alert' does not "
                                  f"parse (got '{alert}')")
    return violations


def _task_after(tasks: list, title: str) -> list:
    for task in tasks:
        if isinstance(task, dict) and task.get("title") == title:
            after = task.get("after", [])
            return list(after) if isinstance(after, list) else []
    return []


def _build(data: dict, name: str,
           fixture: bool = False) -> tuple[dict, list[dict]]:
    """The front line and task lines a validated brief becomes."""
    allocation = dict(data.get("allocation") or {})
    monitors = []
    for entry in data.get("monitor") or []:
        monitors.append({key: entry[key] for key in
                         ("question", "measure", "unit", "of", "every", "alert")
                         if key in entry})
    front = entities.Front(
        id=ids.mint("front"),
        name=name,
        order=data.get("order", 0),
        prefer=data.get("prefer", 0),
        after=[entry for entry in (data.get("after") or [])],
        want=(data.get("want") or "").strip(),
        done_when=(data.get("done-when") or "").strip(),
        land_on=data.get("land-on") or "",
        reviews=data.get("reviews") or "",
        allocation=allocation,
        supervisor=None,
        brief_path=str(paths.brief_path(name)),
        state="queued",
        fixture=fixture,
    )
    front_line = front.to_dict()
    front_line["monitors"] = monitors
    task_lines = []
    for entry in data.get("task") or []:
        task_after = list(entry.get("after") or [])
        task = entities.Task(
            id=ids.mint("task"),
            front=name,
            title=entry.get("title") or "",
            scope=entry.get("scope") or "",
            verify=entry.get("verify") or "",
            size=entry.get("size") or 0,
            after=task_after,
            timeout=entry.get("timeout") or "",
            land_on=entry.get("land-on") or data.get("land-on") or "",
            core=bool(entry.get("core", False)),
            state="ready" if not task_after else "waiting",
            units_done=0,
            units_total=entry.get("size") or 0,
        )
        task_lines.append(task.to_dict())
    return front_line, task_lines


def _read_brief(directory: str, violations: list[str]) -> dict | None:
    src = Path(directory) / "brief.toml"
    try:
        raw = src.read_bytes()
    except FileNotFoundError:
        violations.append(f"brief '{src}' not found")
        return None
    except OSError as exc:
        violations.append(f"brief '{src}' is not readable ({exc})")
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except ValueError as exc:
        violations.append(f"brief '{src}' is not valid TOML ({exc})")
        return None
    if not isinstance(data, dict):
        violations.append(f"brief '{src}' must be a TOML table")
        return None
    return data


def front_add_main(directory: str, dry_run: bool = False,
                   fixture: bool = False) -> int:
    me, violations = caller.resolve("front add")
    caller.check_role(me, "front add", violations=violations)
    data = _read_brief(directory, violations)
    if data is not None:
        violations.extend(_validate(data, _existing_fronts(), directory))
    if violations:
        return Refusal(violations).report()
    assert data is not None
    name = data["name"].strip()
    front_line, task_lines = _build(data, name, fixture=fixture)
    if dry_run:
        print(f"would write {paths.front_record_path(name)}:")
        print(json.dumps(front_line))
        print(f"would write {paths.front_tasks_path(name)}:")
        for line in task_lines:
            print(json.dumps(line))
        print(f"would copy {Path(directory) / 'brief.toml'} -> "
              f"{paths.brief_path(name)}")
        if (Path(directory) / "plan.md").is_file():
            print(f"would copy {Path(directory) / 'plan.md'} -> "
                  f"{paths.plan_path(name)}")
        return 0
    who = caller.by_line(me)
    dest_dir = paths.config_front_dir(name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(directory) / "brief.toml", paths.brief_path(name))
    if (Path(directory) / "plan.md").is_file():
        shutil.copyfile(Path(directory) / "plan.md", paths.plan_path(name))
    store.append_ledger(paths.front_record_path(name), front_line, session_id=who)
    for line in task_lines:
        store.append_ledger(paths.front_tasks_path(name), line, session_id=who)
    print(front_line["id"])
    return 0


def _list_lines() -> list[str]:
    try:
        names = [entry.name for entry in paths.fronts_dir().iterdir()
                 if entry.is_dir()]
    except OSError:
        return []
    rows = []
    for name in names:
        record = read_front_record(name)
        try:
            tasks = store.fold_by_id(
                store.read_ledger(paths.front_tasks_path(name)))
        except OSError:
            tasks = []
        ready = sum(1 for task in tasks if task.get("state") == "ready")
        if record is None:
            rows.append((0, name,
                         f"{name} \u2014 no record \u00b7 tasks {ready}/{len(tasks)} "
                         f"ready \u00b7 waits for unknown"))
            continue
        prefer = record.get("prefer", 0)
        pending = [front for front in (record.get("after") or [])
                   if (read_front_record(front) or {}).get("state") != "done"]
        waits = ", ".join(pending) if pending else "nothing"
        mark = " \u00b7 fixture" if record.get("fixture") else ""
        rows.append((-prefer if isinstance(prefer, int) else 0, name,
                     f"{name}{mark} \u2014 {record.get('state')} "
                     f"\u00b7 prefer {prefer} "
                     f"\u00b7 tasks {ready}/{len(tasks)} ready "
                     f"\u00b7 waits for {waits}"))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in rows]


def front_list_main() -> int:
    me, violations = caller.resolve("front list")
    caller.check_role(me, "front list", violations=violations)
    if violations:
        return Refusal(violations).report()
    for line in _list_lines():
        print(line)
    return 0


def _revised(name: str, violations: list[str]) -> dict | None:
    if not (isinstance(name, str) and name.strip()):
        violations.append("field 'name' is required")
        return None
    record = read_front_record(name.strip())
    if record is None:
        violations.append(f"unknown front '{name.strip()}'")
        return None
    return record


def front_prefer_main(name: str, prefer: str | int) -> int:
    me, violations = caller.resolve("front prefer")
    caller.check_role(me, "front prefer", violations=violations)
    record = _revised(name, violations)
    try:
        number = int(str(prefer).strip())
    except (TypeError, ValueError, AttributeError):
        violations.append(f"field 'prefer' must be an integer (got '{prefer}')")
        number = None
    if violations:
        return Refusal(violations).report()
    assert record is not None and number is not None
    key = name.strip()
    updated = dataclasses.replace(
        entities.Front.from_dict(record), prefer=number).to_dict()
    if "monitors" in record:
        updated["monitors"] = record["monitors"]
    store.append_ledger(paths.front_record_path(key), updated,
                        session_id=caller.by_line(me))
    print(f"{key} prefer {number}")
    return 0


def front_close_main(name: str) -> int:
    me, violations = caller.resolve("front close")
    caller.check_role(me, "front close", violations=violations)
    record = _revised(name, violations)
    if violations:
        return Refusal(violations).report()
    assert record is not None
    key = name.strip()
    updated = dataclasses.replace(
        entities.Front.from_dict(record), state="done").to_dict()
    if "monitors" in record:
        updated["monitors"] = record["monitors"]
    store.append_ledger(paths.front_record_path(key), updated,
                        session_id=caller.by_line(me))
    print(f"{key} closed")
    return 0


def add_front_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="front_verb", required=True)
    add = verbs.add_parser("add", help="Validate a brief and append its front.")
    add.add_argument("directory", help="directory holding brief.toml (and plan.md)")
    add.add_argument("--dry-run", action="store_true",
                     help="validate and print what would be written; write nothing")
    add.add_argument("--fixture", action="store_true",
                     help="a front for trying the runtime out, marked as such "
                          "wherever it appears")
    verbs.add_parser("list", help="Print one line per front.")
    prefer = verbs.add_parser("prefer", help="Set a front's queue preference.")
    prefer.add_argument("name", help="front name")
    prefer.add_argument("prefer", help="new preference (integer)")
    close = verbs.add_parser("close", help="Mark a front done.")
    close.add_argument("name", help="front name")


@cli.subcommand("front", help="Add, list, prefer or close a front.")
def _front_entry(args: argparse.Namespace) -> int:
    if args.front_verb == "add":
        return front_add_main(args.directory, dry_run=args.dry_run,
                              fixture=args.fixture)
    if args.front_verb == "list":
        return front_list_main()
    if args.front_verb == "prefer":
        return front_prefer_main(args.name, args.prefer)
    if args.front_verb == "close":
        return front_close_main(args.name)
    raise AssertionError(f"unknown front verb {args.front_verb!r}")


_front_entry.add_arguments = add_front_arguments  # type: ignore[attr-defined]
