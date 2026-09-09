"""Merge request, land and fail emit wake events across the desk seam.

A real merge request writes exactly one event onto the desk's queue —
merge id, front, branch, no prose — and one collector tick spawns
exactly one turn for that desk. A land (sha on the event) and a fail
do the same in the other direction for the supervisor that asked. A
front whose roster names no desk emits nothing, rather than writing
to a session id nobody holds. An unknown reason is still refused, so
the vocabulary grew rather than opened.

The tick's spawn is the double already used in test_collector_turns:
it refuses anything but the production carrier argv and never starts
a systemd unit.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, paths, store
from foreman import collector as collector_module
from foreman import launch as launch_module
from foreman import wake as wake_module
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.entities import Session

UNIT_RE = re.compile(r"foreman-turn-(.+)-([1-9][0-9]*)")

SUP = "ses-sup0001"
DESK = "ses-desk001"
NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

FLOW_BRIEF = """name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0
merge     = "desk"

[allocation]
muse = 2

[[task]]
title = "first"
scope = \"\"\"
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
\"\"\"
verify = "make check first"
size = 1
after = []
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(launch_module.VENDOR_CONFIG_ENV,
                       str(tmp_path / "home" / "vendor.json"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    return tmp_path


@pytest.fixture()
def turn_spawn(env, monkeypatch):
    """The tick's spawn, doubled at the attribute production calls.

    Same refusals as tests/v5/test_collector_turns.py: anything but a
    systemd-run carrier argv with the per-session per-attempt unit and
    the exact ``foreman turn <session>`` tail, a session the roster
    never minted, or a child world carrying a calling session. What
    passes starts a real detached sleep, reaped on teardown.
    """
    calls: list[dict] = []
    children: list[subprocess.Popen] = []

    def double(argv: list[str], *, env: dict[str, str]):
        assert isinstance(argv, list), argv
        assert argv[0] == "systemd-run", argv
        assert argv[1] == "--user", argv
        assert argv[2].startswith("--unit="), argv
        match = UNIT_RE.fullmatch(argv[2].split("=", 1)[1])
        assert match is not None, argv
        sid, attempt = match.group(1), int(match.group(2))
        setenvs = dict(
            part.split("=", 1)[1].split("=", 1)
            for part in argv[3:] if part.startswith("--setenv="))
        assert setenvs.get("FOREMAN_STATE"), argv
        assert setenvs.get("PYTHONPATH"), argv
        assert SESSION_ENV not in setenvs, argv
        tail = [part for part in argv[3:]
                if not part.startswith("--setenv=")]
        assert tail == [sys.executable, "-m", "foreman", "turn", sid], argv
        assert isinstance(env, dict) and env, argv
        assert SESSION_ENV not in env, argv
        roster = store.read_snapshot(paths.roster_path(),
                                     default={"sessions": {}})
        assert sid in roster.get("sessions", {}), argv
        calls.append({"sid": sid, "attempt": attempt, "unit": match.group(0),
                      "argv": list(argv), "env": dict(env)})
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        children.append(proc)
        return proc

    monkeypatch.setattr(collector_module, "_default_turn_spawn", double)
    try:
        yield calls
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-qm", "seed")
    origin = root / "origin.git"
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    return repo


def make_branch(repo: Path, name: str, filename: str) -> None:
    git(repo, "checkout", "-qb", name)
    (repo / filename).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", filename)
    git(repo, "commit", "-qm", name)
    git(repo, "checkout", "-q", "main")


def write_brief(repo: Path, name: str) -> Path:
    brief_dir = repo / f"brief-{name}"
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        FLOW_BRIEF.format(name=name), encoding="utf-8")
    return brief_dir


def write_check(tmp_path: Path, command: str) -> None:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "foreman.toml").write_text(
        f"[merge]\ncheck = {json.dumps(command)}\n", encoding="utf-8")


def run(monkeypatch, argv, session=None, cwd=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    if cwd is not None:
        monkeypatch.chdir(cwd)
    return cli.main(argv)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def session_record(sid, role, front=None, **fields):
    base = {
        "id": sid, "role": role, "pool": "opus", "model": "opus",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": iso(NOW),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
        "headless": False,
    }
    base.update(fields)
    return Session.from_dict(base).to_dict()


def seed_roster(*entries):
    store.write_snapshot(paths.roster_path(),
                         {"sessions": {entry["id"]: entry
                                       for entry in entries}})


def tasks_by_title(front):
    return {task["title"]: task for task in store.fold_by_id(
        store.read_ledger(paths.front_tasks_path(front)))}


def merges_by_id():
    return {row["id"]: row for row in store.fold_by_id(
        store.read_ledger(paths.merges_path()))}


def build_task(monkeypatch, capsys, front, title, supervisor=SUP,
               moment=NOW):
    task = tasks_by_title(front)[title]
    jid = f"job-{front}-{title}"
    store.append_ledger(paths.front_jobs_path(front), {
        "id": jid, "task": task["id"], "kind": "implement",
        "role": "muse", "priority": 1, "spec_path": "/tmp/specs/job.md",
        "session": None, "worktree": "", "branch": "", "log": "",
        "timeout": "20m", "units": [1], "attempt": 1,
        "state": "returned",
        "planned_at": iso(moment - timedelta(minutes=10)),
        "queued_at": iso(moment - timedelta(minutes=9)),
        "started_at": iso(moment - timedelta(minutes=8)),
        "returned_at": iso(moment - timedelta(minutes=2)),
        "verified_at": None, "artifact": "", "verdict_path": "",
    })
    assert run(monkeypatch, ["job", "verify", jid,
                             "--confirmed",
                             "--command", f"make check {title}",
                             "--output", "ok"], supervisor) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["task", "built", task["id"]],
               supervisor) == 0
    capsys.readouterr()
    return tasks_by_title(front)[title]["id"]


def request_id(monkeypatch, capsys, branch, front, tasks, target,
               supervisor=SUP, cwd=None):
    assert run(monkeypatch, ["merge", "request", branch,
                             "--front", front, "--tasks", *tasks,
                             "--target", target], supervisor, cwd=cwd) == 0
    return capsys.readouterr().out.split()[0]


def pending(sid: str) -> list[dict]:
    return wake_module.pending_events(sid)


def open_front(env, monkeypatch, capsys, name="flow"):
    repo = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "test -f feat.txt")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, name))]) == 0
    capsys.readouterr()
    return repo


def test_merge_request_wakes_the_desk_and_one_tick_carries_it(
        env, turn_spawn, monkeypatch, capsys):
    """A real merge request queues exactly one event on the desk, with
    the merge id, front and branch and no sentence to parse; one tick
    then spawns exactly one turn for that desk, and nobody else."""
    repo = open_front(env, monkeypatch, capsys)
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk", headless=True))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    queued = pending(DESK)
    assert len(queued) == 1
    event = queued[0]
    assert event["reason"] == "merge requested"
    assert event["merge"] == mid
    assert event["front"] == "flow"
    assert event["branch"] == "feat"
    assert event.get("sha") is None
    assert event.get("text") in (None, "")
    assert pending(SUP) == []
    tick(now=NOW)
    assert [(call["sid"], call["attempt"]) for call in turn_spawn] == \
        [(DESK, 1)]


def test_merge_land_wakes_the_supervisor_with_the_sha(
        env, turn_spawn, monkeypatch, capsys):
    """A real land queues exactly one event on the supervisor that
    asked, naming the landed sha; one tick spawns exactly one turn
    for that supervisor. The desk is windowed so its request event
    is not a second carrier."""
    repo = open_front(env, monkeypatch, capsys)
    seed_roster(session_record(SUP, "supervisor", "flow", headless=True),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 0
    capsys.readouterr()
    head = merges_by_id()[mid]["head"]
    assert re.fullmatch(r"[0-9a-f]{40}", head)
    queued = pending(SUP)
    assert len(queued) == 1
    event = queued[0]
    assert event["reason"] == "merge landed"
    assert event["merge"] == mid
    assert event["front"] == "flow"
    assert event["branch"] == "feat"
    assert event["sha"] == head
    assert event.get("text") in (None, "")
    tick(now=NOW)
    assert [(call["sid"], call["attempt"]) for call in turn_spawn] == \
        [(SUP, 1)]


def test_merge_fail_wakes_the_supervisor(
        env, turn_spawn, monkeypatch, capsys):
    """A real fail queues exactly one event on the supervisor that
    asked, with the merge id, front and branch and no sha; one tick
    spawns exactly one turn for that supervisor."""
    repo = open_front(env, monkeypatch, capsys)
    seed_roster(session_record(SUP, "supervisor", "flow", headless=True),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "fail", mid,
                             "--reason", "the check needs a fixture"],
               DESK) == 0
    capsys.readouterr()
    queued = pending(SUP)
    assert len(queued) == 1
    event = queued[0]
    assert event["reason"] == "merge failed"
    assert event["merge"] == mid
    assert event["front"] == "flow"
    assert event["branch"] == "feat"
    assert event.get("sha") is None
    assert event.get("text") in (None, "")
    tick(now=NOW)
    assert [(call["sid"], call["attempt"]) for call in turn_spawn] == \
        [(SUP, 1)]


def test_no_desk_on_the_roster_emits_nothing(
        env, turn_spawn, monkeypatch, capsys):
    """A front whose roster names no merge-desk session still accepts
    the request, writes no event, and a tick spawns no turn. The desk
    id used in the other tests is never created."""
    repo = open_front(env, monkeypatch, capsys)
    seed_roster(session_record(SUP, "supervisor", "flow"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert merges_by_id()[mid]["result"] == "requested"
    assert pending(SUP) == []
    assert pending(DESK) == []
    assert not paths.session_events_path(DESK).exists()
    tick(now=NOW)
    assert turn_spawn == []


def test_unknown_reason_is_still_refused(env):
    """The vocabulary grew by three named reasons; a misspelling of
    one of them is still refused rather than written."""
    seed_roster(session_record(SUP, "supervisor", "flow", headless=True))
    with pytest.raises(ValueError) as caught:
        wake_module.append_event(SUP, "merge requestd", merge="mrg-x")
    assert "merge requestd" in str(caught.value)
    assert pending(SUP) == []
    for reason in ("merge requested", "merge landed", "merge failed"):
        assert reason in entities.WAKE_REASONS
        assert wake_module.append_event(SUP, reason)["reason"] == reason


def test_a_desk_rostered_onto_another_front_is_not_woken(
        env, turn_spawn, monkeypatch, capsys):
    """A desk that holds a different front does not serve this one.

    One desk with no front serves the swarm; a desk rostered onto a
    front serves that front alone. A request on 'flow' with only an
    'other' desk on the roster writes nothing, and the heartbeat
    remains the only thing that will reach it.
    """
    repo = open_front(env, monkeypatch, capsys)
    other = "ses-desk002"
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(other, "merge-desk", "other",
                               headless=True))
    first = build_task(monkeypatch, capsys, "flow", "first")
    request_id(monkeypatch, capsys, "feat", "flow", [first],
               "main", cwd=repo)
    assert wake_module.desk_for("flow") is None
    assert pending(other) == []
    assert not paths.session_events_path(other).exists()
    tick(now=NOW)
    assert turn_spawn == []


def test_a_live_desk_is_woken_ahead_of_an_exited_one(
        env, turn_spawn, monkeypatch, capsys):
    """Two desks on the roster, one exited: the running one is woken.

    The exited desk sorts first by id, so a chooser that took the
    first match would write to a session that will never turn again.
    """
    repo = open_front(env, monkeypatch, capsys)
    dead, live = "ses-desk000", "ses-desk999"
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(dead, "merge-desk", headless=True,
                               state="exited"),
                session_record(live, "merge-desk", headless=True,
                               state="running"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert wake_module.desk_for("flow") == live
    assert [event["merge"] for event in pending(live)] == [mid]
    assert pending(dead) == []
    tick(now=NOW)
    assert [(call["sid"], call["attempt"]) for call in turn_spawn] == \
        [(live, 1)]


def test_a_requester_the_roster_forgot_is_never_written_to(
        env, monkeypatch, capsys):
    """A merge whose requester has left the roster wakes nobody.

    The land still lands and the fail still fails: the seam is a
    courtesy, and a session id nobody holds gets no ledger of its own.
    """
    repo = open_front(env, monkeypatch, capsys)
    write_check(env, "test -f feat.txt")
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk", headless=True))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    gone = merges_by_id()[mid]["by"]
    assert gone and gone != DESK
    seed_roster(session_record(DESK, "merge-desk", headless=True))
    assert wake_module._rostered_session(gone) is None
    assert run(monkeypatch, ["merge", "fail", mid,
                             "--reason", "the check went red"],
               DESK, cwd=repo) == 0
    capsys.readouterr()
    assert merges_by_id()[mid]["result"] == "failed"
    assert not paths.session_events_path(gone).exists()
