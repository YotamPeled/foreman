"""Clause 1.4: unknown --task is refused; job repoint moves a verified job."""

from __future__ import annotations

import json
import os
from pathlib import Path

CLAUSE = ("1.4", "task refused")

_SPEC = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v0 -q\n"
)

_BRIEF = '''name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 2

[[task]]
title = "the real one"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "make check first"
size = 3
after = []

[[task]]
title = "the other one"
scope = """
WHAT: do the second thing.
INPUTS: the first artifact.
OUTPUTS: the second artifact.
OUT OF SCOPE: everything else.
"""
verify = "make check second"
size = 2
after = []
'''


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _fold(rows: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in rows:
        key = row.get("id")
        if isinstance(key, str) and key:
            latest[key] = row
    return latest


def _state() -> Path:
    return Path(os.environ["FOREMAN_STATE"])


def _add_front(world, name: str) -> None:
    directory = Path(world["specs"]) / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _BRIEF.format(name=name), encoding="utf-8")
    added = run_foreman(["front", "add", str(directory)])
    if added.returncode != 0:
        raise Failure(f"front add {name} failed: {added.stderr[-800:]}")


def _tasks_by_title(front: str) -> dict[str, dict]:
    rows = _fold(_read_jsonl(_state() / "fronts" / front / "tasks.jsonl"))
    return {row["title"]: row for row in rows.values() if row.get("title")}


def apply(world) -> None:
    front = world.get("_14_front")
    if not front:
        return
    tasks = _tasks_by_title(front)
    real = tasks.get("the real one")
    if real is None:
        return
    path = _state() / "fronts" / front / "tasks.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(real, title="no such")) + "\n")


def restore(world) -> None:
    front = world.get("_14_front")
    if not front:
        return
    path = _state() / "fronts" / front / "tasks.jsonl"
    rows = [row for row in _read_jsonl(path) if row.get("title") != "no such"]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


BREAK = ("add a task titled 'no such' so the unknown-task refusal is skipped",
         apply, restore)


def run(world) -> str:
    assert_world()
    n = world.get("_14_n", 0) + 1
    world["_14_n"] = n
    name = world.get("_14_front")
    if not name:
        name = f"ref{n}"
        world["_14_front"] = name
        _add_front(world, name)
    spec = Path(world["specs"]) / "task-refused.md"
    spec.write_text(_SPEC, encoding="utf-8")
    refused = run_foreman([
        "launch", "muse", "fake", str(spec),
        "--repo", str(world["repo_a"]),
        "--front", name,
        "--task", "no such",
        "--effort", "xhigh",
        "--dry-run",
    ])
    if refused.returncode == 0:
        raise Failure("launch --task 'no such' was admitted")
    err = refused.stderr or ""
    if f"--task 'no such' names no task on front '{name}'" not in err:
        raise Failure(f"refusal did not name the title: {err[-800:]}")
    if "nearest: 'the real one'" not in err:
        raise Failure(f"refusal did not name the nearest title: {err[-800:]}")
    tasks = _tasks_by_title(name)
    first, other = tasks["the real one"], tasks["the other one"]
    jid = f"job-rep{n}"
    jobs_path = _state() / "fronts" / name / "jobs.jsonl"
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(json.dumps({
        "id": jid, "task": first["id"], "kind": "implement", "role": "muse",
        "priority": 1, "spec_path": "", "session": "ses-wrk0001",
        "worktree": "", "branch": "foreman/job", "log": "", "timeout": "20m",
        "units": 3, "attempt": 1, "state": "verified",
        "planned_at": "2026-09-08T12:00:00+00:00",
        "queued_at": "2026-09-08T12:01:00+00:00",
        "started_at": "2026-09-08T12:02:00+00:00",
        "returned_at": "2026-09-08T12:08:00+00:00",
        "verified_at": "2026-09-08T12:09:00+00:00",
        "artifact": "", "verdict_path": "",
    }) + "\n", encoding="utf-8")
    moved = run_foreman([
        "job", "repoint", jid, "--task", "the other one",
        "--reason", "credited to the wrong task",
    ])
    if moved.returncode != 0:
        raise Failure(f"job repoint failed: {moved.stderr[-800:]}")
    want = f"job {jid}: task {first['id']} -> {other['id']}"
    if want not in moved.stdout:
        raise Failure(f"repoint printed {moved.stdout!r}, not {want!r}")
    return (
        f"launch --task 'no such' nearest 'the real one'; "
        f"repoint {jid} {first['id']} -> {other['id']}"
    )
