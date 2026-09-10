"""Clause 1.6: job verify --run scratches under the world's state directory."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLAUSE = ("1.6", "no tmp")

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
title = "first"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "printf '%s\\n' \\"$PWD\\""
size = 1
after = []
'''


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(
            f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout)[-400:]}")
    return proc.stdout.strip()


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


def apply(world) -> None:
    jid = world.get("_16_job")
    if not jid:
        return
    jobs = _state() / "fronts" / world["_16_front"] / "jobs.jsonl"
    rows = _read_jsonl(jobs)
    for row in rows:
        if row.get("id") == jid:
            row["head"] = "0" * 40
            row["branch"] = "missing-branch"
    jobs.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def restore(world) -> None:
    saved = world.get("_16_jobs")
    front = world.get("_16_front")
    if saved is None or not front:
        return
    path = _state() / "fronts" / front / "jobs.jsonl"
    path.write_text(saved, encoding="utf-8")


BREAK = ("point the fixture job at a missing branch so --run cannot scratch",
         apply, restore)


def run(world) -> str:
    assert_world()
    n = world.get("_16_n", 0) + 1
    world["_16_n"] = n
    name = world.get("_16_front")
    if not name:
        name = f"tmp{n}"
        world["_16_front"] = name
        directory = Path(world["specs"]) / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "brief.toml").write_text(
            _BRIEF.format(name=name), encoding="utf-8")
        added = run_foreman(["front", "add", str(directory)])
        if added.returncode != 0:
            raise Failure(f"front add {name} failed: {added.stderr[-800:]}")
        tasks = _fold(_read_jsonl(_state() / "fronts" / name / "tasks.jsonl"))
        first = next(row for row in tasks.values() if row.get("title") == "first")
        repo = Path(world["repo_a"]).parent / f"m1-notmp-{n}"
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "proof@example.invalid")
        _git(repo, "config", "user.name", "proof")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "root")
        _git(repo, "checkout", "-qb", "job-branch")
        (repo / "built").write_text("the artifact\n", encoding="utf-8")
        _git(repo, "add", "built")
        _git(repo, "commit", "-qm", "the work")
        head = _git(repo, "rev-parse", "HEAD")
        sid = f"ses-notmp{n}"
        (_state() / "sessions" / sid).mkdir(parents=True, exist_ok=True)
        jid = f"job-notmp{n}"
        world["_16_job"] = jid
        jobs_path = _state() / "fronts" / name / "jobs.jsonl"
        record = {
            "id": jid, "task": first["id"], "kind": "implement", "role": "muse",
            "priority": 1, "spec_path": "", "session": sid,
            "worktree": str(repo), "branch": "job-branch", "head": head,
            "log": "", "timeout": "20m", "units": 1, "attempt": 1,
            "state": "returned",
            "planned_at": "2026-09-08T12:00:00+00:00",
            "queued_at": "2026-09-08T12:01:00+00:00",
            "started_at": "2026-09-08T12:02:00+00:00",
            "returned_at": "2026-09-08T12:08:00+00:00",
            "verified_at": None, "artifact": "", "verdict_path": "",
        }
        jobs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        world["_16_jobs"] = jobs_path.read_text(encoding="utf-8")
    jid = world["_16_job"]
    jobs_path = _state() / "fronts" / name / "jobs.jsonl"
    current = _fold(_read_jsonl(jobs_path)).get(jid)
    if current is None:
        raise Failure(f"fixture job {jid} is missing")
    current["state"] = "returned"
    current["verified_at"] = None
    for key in ("verify_command", "verify_exit", "verify_seconds",
                "verify_output_ref"):
        current.pop(key, None)
    jobs_path.write_text(json.dumps(current) + "\n", encoding="utf-8")
    ran = run_foreman(["job", "verify", jid, "--run"])
    if ran.returncode != 0:
        raise Failure(
            f"job verify --run failed: "
            f"{(ran.stdout or '')[-400:]} {(ran.stderr or '')[-800:]}")
    job = _fold(_read_jsonl(jobs_path))[jid]
    dest = Path(job.get("verify_output_ref") or "")
    if not dest.is_file():
        raise Failure(f"verify wrote no output file {dest}")
    cwd = Path(dest.read_text(encoding="utf-8").strip())
    state = _state().resolve()
    try:
        cwd.resolve().relative_to(state)
    except ValueError as exc:
        raise Failure(
            f"verify cwd {cwd} is not under the state directory {state}"
        ) from exc
    if "/tmp/" in str(cwd):
        raise Failure(f"verify cwd {cwd} is under /tmp")
    if "worktrees/scratch/verify-" not in str(cwd):
        raise Failure(f"verify cwd {cwd} is not a scratch verify worktree")
    if cwd.exists():
        raise Failure(f"scratch worktree {cwd} was left behind")
    return f"verify --run scratched at {cwd} under state and removed it"
