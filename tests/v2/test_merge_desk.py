"""The merge desk: requests, the FCFS queue, landing through the desk.

Every test drives the real entry points against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``, with a real git repository the
test builds: branches that really rebase, a check command that really runs,
a bare remote that really takes the push. No test writes to a real state
directory and none pushes anywhere outside ``tmp_path``.

The break each test catches is a branch landing without a rebase, without
its check or without its push, a queue that is not first come first served,
a supervisor landing past the desk, or a desk summoned twice.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, paths, procs, store, wake
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
DESK = "ses-desk001"
DESK2 = "ses-desk002"
WRK = "ses-wrk0001"
GHOST = "ses-ghost01"

FLOW_BRIEF = """name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0
{merge}
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

[[task]]
title = "second"
scope = \"\"\"
WHAT: do the second thing.
INPUTS: the first artifact.
OUTPUTS: the second artifact.
OUT OF SCOPE: everything else.
\"\"\"
verify = "make check second"
size = 1
after = ["first"]
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
    monkeypatch.setattr(launch_module, "WINDOW_LINGER_SECONDS", 5)
    yield tmp_path
    _kill_windows(tmp_path)


def _kill_windows(root: Path) -> None:
    sessions = store.read_snapshot(root / "state" / "roster.json",
                                   default={"sessions": {}})
    for record in (sessions.get("sessions") or {}).values():
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        if pid != record.get("pgid") or pid == os.getpid():
            continue
        if not procs.same_process(pid, record.get("pid_starttime")):
            continue
        procs.kill_job(pid, record.get("pgid"))
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def make_repo(root: Path) -> tuple[Path, Path]:
    """A repo with a bare origin beside it, both under tmp_path."""
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
    return repo, origin


def make_branch(repo: Path, name: str, filename: str) -> None:
    git(repo, "checkout", "-qb", name)
    (repo / filename).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", filename)
    git(repo, "commit", "-qm", name)
    git(repo, "checkout", "-q", "main")


def write_brief(repo: Path, name: str, merge: str | None = "desk") -> Path:
    """A brief for this module, which is about the desk.

    `merge` defaults to "desk" here because that is what these tests are
    for. In a real brief the field is absent and the front lands itself:
    the desk is a product swarm's discipline, and a front that declares no
    desk must not be stopped from landing by one.
    """
    brief_dir = repo / f"brief-{name}"
    brief_dir.mkdir(parents=True, exist_ok=True)
    merge_line = f'merge     = "{merge}"\n' if merge else ""
    (brief_dir / "brief.toml").write_text(
        FLOW_BRIEF.format(name=name, merge=merge_line), encoding="utf-8")
    return brief_dir


def write_check(tmp_path: Path, command: str) -> None:
    write_merge_config(tmp_path, fallback=command)


def write_merge_config(tmp_path: Path, fallback: str | None = None,
                       per_repo: dict[str | Path, str] | None = None) -> None:
    """Write ``[merge]`` and any ``[merge."<path>"]`` check tables."""
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    if fallback is not None:
        parts.append("[merge]")
        parts.append(f"check = {json.dumps(fallback)}")
        parts.append("")
    for repo_path, command in (per_repo or {}).items():
        key = os.path.realpath(str(repo_path))
        parts.append(f"[merge.{json.dumps(key)}]")
        parts.append(f"check = {json.dumps(command)}")
        parts.append("")
    (config / "foreman.toml").write_text(
        "\n".join(parts) if parts else "# no [merge] table here\n",
        encoding="utf-8")


def moving_target_check(origin: Path) -> str:
    """One line of shell: push a commit onto the origin's target.

    Each run clones into a fresh temp dir so a second land can move
    the target again without colliding with the first clone.
    """
    return (
        f"d=$(mktemp -d) && "
        f"git clone --quiet {origin} \"$d\" && "
        f"git -C \"$d\" config user.email test@example.invalid && "
        f"git -C \"$d\" config user.name test && "
        f"echo during-$(date +%s%N) > \"$d\"/during.txt && "
        f"git -C \"$d\" add during.txt && "
        f"git -C \"$d\" commit -qm during-check && "
        f"git -C \"$d\" push origin HEAD:refs/heads/main"
    )


def pending_ids(sid: str) -> set[str]:
    return {event["id"] for event in wake.pending_events(sid)
            if isinstance(event.get("id"), str)}


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
        "started_at": iso(NOW - timedelta(minutes=30)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }
    base.update(fields)
    return base


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
    """A task through verify and built, the way flow 2 leaves it."""
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


# --------------------------------------------------------------------------
# Flow 2 through the desk
# --------------------------------------------------------------------------


def test_flow_2_request_take_land(env, monkeypatch, capsys):
    """Built, requested, taken, landed: the task reads landed with the
    head the rebase produced, on top of a target that moved mid-queue."""
    repo, origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "test -f feat.txt")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    before_tasks = len(store.read_ledger(paths.front_tasks_path("flow")))

    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    record = merges_by_id()[mid]
    assert record["front"] == "flow"
    assert record["branch"] == "feat"
    assert record["tasks"] == [first]
    assert record["target"] == "main"
    assert record["result"] == "requested"

    # The target moves while the record waits: landing must carry it.
    git(repo, "checkout", "-q", "main")
    (repo / "moved.txt").write_text("moved\n", encoding="utf-8")
    git(repo, "add", "moved.txt")
    git(repo, "commit", "-qm", "target moves")
    git(repo, "push", "-q", "origin", "main")
    old_head = git(repo, "rev-parse", "feat")

    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    assert merges_by_id()[mid]["result"] == "merging"

    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 0
    out = capsys.readouterr().out
    head = git(repo, "rev-parse", "feat")
    assert head != old_head
    assert head in out
    assert git(repo, "merge-base", "--is-ancestor", "main", "feat") == ""
    assert (repo / "moved.txt").is_file()
    # Pushed: the bare origin holds the rebased head on the target
    # and on the branch.
    assert git(origin, "rev-parse", "refs/heads/main") == head
    assert git(origin, "rev-parse", "refs/heads/feat") == head

    record = merges_by_id()[mid]
    assert record["result"] == "landed"
    assert record["head"] == head
    assert tasks_by_title("flow")["first"]["state"] == "landed"
    assert tasks_by_title("flow")["first"]["head"] == head
    assert tasks_by_title("flow")["second"]["state"] == "ready"
    # Appends, not edits: landing grew the task ledger, and the merge
    # ledger holds the request, the take and the landing.
    assert len(store.read_ledger(
        paths.front_tasks_path("flow"))) == before_tasks + 2
    assert len(store.read_ledger(paths.merges_path())) == 3

    assert run(monkeypatch, ["status"], cwd=repo) == 0
    status = capsys.readouterr().out
    assert f"landed: feat -> main at {head}" in status


def test_status_shows_a_waiting_record(env, monkeypatch, capsys):
    """The queue block names the front, branch, target, tasks and wait."""
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "true")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    request_id(monkeypatch, capsys, "feat", "flow", [first],
               "main", cwd=repo)
    assert run(monkeypatch, ["status"], cwd=repo) == 0
    status = capsys.readouterr().out
    assert "waiting: flow: feat -> main, lands first (requested " in status


# --------------------------------------------------------------------------
# Request refusals
# --------------------------------------------------------------------------


def test_request_names_every_violation_at_once(env, monkeypatch, capsys):
    """A request wrong in four ways is told all four, not the first."""
    repo, _origin = make_repo(env)
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    write_brief(repo, "other")
    assert run(monkeypatch, ["front", "add",
                             str(repo / "brief-other")]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record("ses-other", "supervisor", "other"))
    build_task(monkeypatch, capsys, "other", "first",
               supervisor="ses-other")
    foreign = tasks_by_title("other")["first"]["id"]
    before = len(store.read_ledger(paths.merges_path()))
    rc = run(monkeypatch, ["merge", "request", "no-such-branch",
                           "--front", "flow",
                           "--tasks", "second", foreign,
                           "--target", "no-such-target"], SUP, cwd=repo)
    assert rc == 1
    _, err = capsys.readouterr()
    assert "'no-such-branch'" in err
    assert "'no-such-target'" in err
    assert "second" in err and "not 'built'" in err
    assert foreign in err and "'other'" in err
    assert len(store.read_ledger(paths.merges_path())) == before


def test_request_missing_fields_are_all_named(env, monkeypatch, capsys):
    repo, _origin = make_repo(env)
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"))
    assert run(monkeypatch, ["merge", "request"], SUP, cwd=repo) == 1
    _, err = capsys.readouterr()
    assert "'branch'" in err
    assert "'--front'" in err
    assert "'--tasks'" in err
    assert "'--target'" in err


def test_request_on_self_mode_front_is_refused(env, monkeypatch, capsys):
    """A front whose record carries `merge = ""` is refused at request
    time: the message names `task landed`, no merge record is minted,
    and the desk's wake queue gains no event."""
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "true")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "plain", merge=None))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "plain"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "plain", "first")
    before = len(store.read_ledger(paths.merges_path()))
    rc = run(monkeypatch, ["merge", "request", "feat",
                           "--front", "plain", "--tasks", first,
                           "--target", "main"], SUP, cwd=repo)
    assert rc == 1
    _, err = capsys.readouterr()
    assert "task landed" in err
    assert len(store.read_ledger(paths.merges_path())) == before
    assert wake.pending_events(DESK) == []


def test_self_mode_refusal_arrives_with_other_violations(
        env, monkeypatch, capsys):
    """A self-mode front that is also missing a field is told both,
    not the first alone."""
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "plain", merge=None))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "plain"))
    first = build_task(monkeypatch, capsys, "plain", "first")
    rc = run(monkeypatch, ["merge", "request", "feat",
                           "--front", "plain", "--tasks", first],
             SUP, cwd=repo)
    assert rc == 1
    _, err = capsys.readouterr()
    assert "task landed" in err
    assert "'--target'" in err
    assert store.read_ledger(paths.merges_path()) == []


def test_request_refused_for_anyone_but_the_front_supervisor(
        env, monkeypatch, capsys):
    """A worker, the desk and a stranger are all refused by role or name."""
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "true")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(WRK, "muse", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    before = len(store.read_ledger(paths.merges_path()))
    assert run(monkeypatch, ["merge", "request", "feat",
                             "--front", "flow", "--tasks", first,
                             "--target", "main"], WRK, cwd=repo) == 1
    assert "may not call 'merge request'" in capsys.readouterr().err
    assert run(monkeypatch, ["merge", "request", "feat",
                             "--front", "flow", "--tasks", first,
                             "--target", "main"], DESK, cwd=repo) == 1
    assert "may not call 'merge request'" in capsys.readouterr().err
    assert run(monkeypatch, ["merge", "request", "feat",
                             "--front", "flow", "--tasks", first,
                             "--target", "main"], GHOST, cwd=repo) == 1
    assert "unregistered writer" in capsys.readouterr().err
    assert len(store.read_ledger(paths.merges_path())) == before


# --------------------------------------------------------------------------
# Take: first come, first served
# --------------------------------------------------------------------------


def test_take_is_first_come_first_served(env, monkeypatch, capsys):
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "true")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"),
                session_record(DESK2, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    older = request_id(monkeypatch, capsys, "feat", "flow", [first],
                       "main", cwd=repo)
    newer = request_id(monkeypatch, capsys, "feat", "flow", [first],
                       "main", cwd=repo)

    assert run(monkeypatch, ["merge", "take", newer], DESK) == 1
    err = capsys.readouterr().err
    assert older in err and newer in err

    assert run(monkeypatch, ["merge", "take", older], DESK) == 0
    capsys.readouterr()
    # The same session retaking is a no-op, not a refusal.
    assert run(monkeypatch, ["merge", "take", older], DESK) == 0
    assert "already held" in capsys.readouterr().out
    # Another live session is refused by the holder's name.
    assert run(monkeypatch, ["merge", "take", older], DESK2) == 1
    assert DESK in capsys.readouterr().err

    assert run(monkeypatch, ["merge", "take", newer], DESK) == 0
    capsys.readouterr()

    assert run(monkeypatch, ["merge", "take", "mrg-nope"], DESK) == 1
    assert "unknown merge" in capsys.readouterr().err
    # A supervisor may not take: the queue is the desk's.
    assert run(monkeypatch, ["merge", "take", newer], SUP) == 1
    assert "may not call 'merge take'" in capsys.readouterr().err


def test_land_requires_a_take_first(env, monkeypatch, capsys):
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "true")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 1
    assert "take it first" in capsys.readouterr().err
    assert tasks_by_title("flow")["first"]["state"] == "built"


def test_land_without_a_check_command_is_refused(env, monkeypatch, capsys):
    """The desk lands nothing it cannot check, and names the key to set."""
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_merge_config(env)
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 1
    err = capsys.readouterr().err
    resolved = os.path.realpath(repo)
    assert f"no check for repository {resolved}" in err
    assert str(paths.config_file()) in err
    assert f'[merge."{resolved}"] check = "<cmd>"' in err
    assert "[merge] check" in err
    assert tasks_by_title("flow")["first"]["state"] == "built"
    assert merges_by_id()[mid]["result"] == "merging"


def test_land_runs_the_check_for_the_target_repository(
        env, monkeypatch, capsys):
    """Two repositories, two checks: each landing runs its own, never
    the ``[merge] check`` fallback of ``false``."""
    from foreman.merge import merge_check_command

    repo_a, _origin_a = make_repo(env / "world-a")
    repo_b, _origin_b = make_repo(env / "world-b")
    marker_a = env / "checked-a"
    marker_b = env / "checked-b"
    write_merge_config(
        env, fallback="false",
        per_repo={
            repo_a: f"touch {json.dumps(str(marker_a))}",
            repo_b: f"touch {json.dumps(str(marker_b))}",
        })
    assert merge_check_command(str(repo_a)).startswith("touch ")
    assert merge_check_command(str(repo_b)).startswith("touch ")
    assert "checked-a" in merge_check_command(str(repo_a))
    assert "checked-b" in merge_check_command(str(repo_b))
    make_branch(repo_a, "feat", "feat.txt")
    make_branch(repo_b, "feat", "feat.txt")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo_a, "alpha"))]) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo_b, "beta"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "alpha"),
                session_record("ses-supbeta", "supervisor", "beta"),
                session_record(DESK, "merge-desk"))
    first_a = build_task(monkeypatch, capsys, "alpha", "first")
    first_b = build_task(monkeypatch, capsys, "beta", "first",
                         supervisor="ses-supbeta")
    mid_a = request_id(monkeypatch, capsys, "feat", "alpha", [first_a],
                       "main", cwd=repo_a)
    mid_b = request_id(monkeypatch, capsys, "feat", "beta", [first_b],
                       "main", cwd=repo_b, supervisor="ses-supbeta")
    assert run(monkeypatch, ["merge", "take", mid_a], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "land", mid_a], DESK, cwd=repo_a) == 0
    capsys.readouterr()
    assert marker_a.is_file()
    assert not marker_b.is_file()
    assert run(monkeypatch, ["merge", "take", mid_b], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "land", mid_b], DESK, cwd=repo_b) == 0
    capsys.readouterr()
    assert marker_b.is_file()
    assert merges_by_id()[mid_a]["result"] == "landed"
    assert merges_by_id()[mid_b]["result"] == "landed"


def test_land_uses_the_fallback_check_when_the_repository_has_no_table(
        env, monkeypatch, capsys):
    """A third repository with no table of its own runs ``[merge] check``."""
    from foreman.merge import merge_check_command

    other, _origin = make_repo(env / "world-c")
    named, _named_origin = make_repo(env / "world-named")
    marker = env / "checked-fallback"
    write_merge_config(
        env, fallback=f"touch {json.dumps(str(marker))}",
        per_repo={named: "false"})
    assert merge_check_command(str(other)) == f"touch {json.dumps(str(marker))}"
    make_branch(other, "feat", "feat.txt")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(other, "gamma"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "gamma"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "gamma", "first")
    mid = request_id(monkeypatch, capsys, "feat", "gamma", [first],
                     "main", cwd=other)
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=other) == 0
    capsys.readouterr()
    assert marker.is_file()
    assert merges_by_id()[mid]["result"] == "landed"


def test_land_failing_check_leaves_the_tasks_built(env, monkeypatch, capsys):
    """A red check is a broken deliverable, not a race: the land
    refuses, the record stays taken, and nobody is woken."""
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "test -f this-file-is-not-there")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    desk_before = pending_ids(DESK)
    sup_before = pending_ids(SUP)
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 1
    err = capsys.readouterr().err
    assert "failed" in err
    assert tasks_by_title("flow")["first"]["state"] == "built"
    record = merges_by_id()[mid]
    assert record["result"] == "merging"
    assert record.get("taken_by") == DESK
    assert not record.get("land_attempts")
    assert pending_ids(DESK) == desk_before
    assert pending_ids(SUP) == sup_before


def test_land_advances_the_target(env, monkeypatch, capsys):
    """After land, the origin's target ref is the landed head, not the
    sha it started at; the branch and the task record match it."""
    repo, origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "test -f feat.txt")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    old_main = git(origin, "rev-parse", "refs/heads/main")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 0
    capsys.readouterr()
    head = git(repo, "rev-parse", "feat")
    assert head != old_main
    assert git(origin, "rev-parse", "refs/heads/main") == head
    assert git(origin, "rev-parse", "refs/heads/feat") == head
    assert git(repo, "rev-parse", "feat") == head
    record = merges_by_id()[mid]
    assert record["result"] == "landed"
    assert record["head"] == head
    assert tasks_by_title("flow")["first"]["state"] == "landed"
    assert tasks_by_title("flow")["first"]["head"] == head


def test_land_is_a_fast_forward(env, monkeypatch, capsys):
    """The target's new tip has the old target as its first parent, and
    the landed range contains no merge commit."""
    repo, origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "test -f feat.txt")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    old_main = git(origin, "rev-parse", "refs/heads/main")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 0
    capsys.readouterr()
    head = git(origin, "rev-parse", "refs/heads/main")
    assert git(origin, "rev-parse", f"{head}^") == old_main
    parents = subprocess.run(
        ["git", "--git-dir", str(origin), "cat-file", "-p", head],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert parents.returncode == 0
    parent_lines = [line for line in parents.stdout.splitlines()
                    if line.startswith("parent ")]
    assert parent_lines == [f"parent {old_main}"]
    assert git(origin, "rev-list", "--merges", f"{old_main}..{head}") == ""


def test_land_refuses_when_the_target_moved_during_the_check(
        env, monkeypatch, capsys):
    """A check that pushes a new commit onto the origin's target leaves
    land refusing: origin stays at that commit, the branch is unmoved
    by us, the record goes back to requested with attempt 1 and nobody
    holding it, and the desk's queue holds one fresh merge requested."""
    repo, origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, moving_target_check(origin))
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    old_main = git(origin, "rev-parse", "refs/heads/main")
    old_feat = git(repo, "rev-parse", "feat")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    desk_before = pending_ids(DESK)
    sup_before = pending_ids(SUP)
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 1
    err = capsys.readouterr().err
    found = git(origin, "rev-parse", "refs/heads/main")
    assert found != old_main
    assert git(origin, "show", "refs/heads/main:during.txt").startswith("during")
    missing_feat = subprocess.run(
        ["git", "--git-dir", str(origin), "cat-file", "-e",
         "refs/heads/main:feat.txt"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert missing_feat.returncode != 0
    assert old_main in err
    assert found in err
    assert "push of 'feat'" not in err
    assert git(repo, "rev-parse", "feat") == old_feat
    absent = subprocess.run(
        ["git", "--git-dir", str(origin), "show-ref", "--verify", "--quiet",
         "refs/heads/feat"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert absent.returncode != 0
    record = merges_by_id()[mid]
    assert record["result"] == "requested"
    assert record["land_attempts"] == 1
    assert record.get("taken_by") in (None, "")
    assert old_main in (record.get("fail_reason") or "")
    assert found in (record.get("fail_reason") or "")
    assert tasks_by_title("flow")["first"]["state"] == "built"
    fresh = [event for event in wake.pending_events(DESK)
             if event.get("id") not in desk_before]
    assert len(fresh) == 1
    assert fresh[0]["reason"] == "merge requested"
    assert fresh[0]["merge"] == mid
    assert pending_ids(SUP) == sup_before


def test_a_second_target_moved_land_fails_the_record(
        env, monkeypatch, capsys):
    """The same landing against a target that moved again fails the
    record with the reason, and the supervisor's queue holds merge
    failed. The desk is not woken a second time."""
    repo, origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, moving_target_check(origin))
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 1
    capsys.readouterr()
    assert merges_by_id()[mid]["result"] == "requested"
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    desk_before = pending_ids(DESK)
    sup_before = pending_ids(SUP)
    old_feat = git(repo, "rev-parse", "feat")
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 1
    err = capsys.readouterr().err
    found = git(origin, "rev-parse", "refs/heads/main")
    record = merges_by_id()[mid]
    assert record["result"] == "failed"
    assert record["failed_at"]
    assert found in (record.get("fail_reason") or "")
    assert found in err
    assert git(repo, "rev-parse", "feat") == old_feat
    assert tasks_by_title("flow")["first"]["state"] == "built"
    fresh_desk = [event for event in wake.pending_events(DESK)
                  if event.get("id") not in desk_before]
    assert fresh_desk == []
    fresh_sup = [event for event in wake.pending_events(SUP)
                 if event.get("id") not in sup_before]
    assert len(fresh_sup) == 1
    assert fresh_sup[0]["reason"] == "merge failed"
    assert fresh_sup[0]["merge"] == mid


def test_land_succeeds_on_the_second_attempt_and_clears_it(
        env, monkeypatch, capsys):
    """After a target-moved re-queue, a land whose check does not move
    the target lands normally and the attempt is gone."""
    repo, origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, moving_target_check(origin))
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    old_main = git(origin, "rev-parse", "refs/heads/main")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 1
    capsys.readouterr()
    assert merges_by_id()[mid]["land_attempts"] == 1
    git(origin, "update-ref", "refs/heads/main", old_main)
    write_check(env, "true")
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo) == 0
    capsys.readouterr()
    record = merges_by_id()[mid]
    assert record["result"] == "landed"
    assert record.get("land_attempts") in (0, None)
    assert record.get("fail_reason") in (None, "")
    head = git(origin, "rev-parse", "refs/heads/main")
    assert record["head"] == head
    assert tasks_by_title("flow")["first"]["state"] == "landed"
    assert tasks_by_title("flow")["first"]["head"] == head


# --------------------------------------------------------------------------
# Fail: the tasks stay built, the branch is left alone
# --------------------------------------------------------------------------


def test_fail_keeps_tasks_built_and_branch_alone(env, monkeypatch, capsys):
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "true")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    mid = request_id(monkeypatch, capsys, "feat", "flow", [first],
                     "main", cwd=repo)
    sha_before = git(repo, "rev-parse", "feat")
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()

    assert run(monkeypatch, ["merge", "fail", mid], DESK) == 1
    assert "'--reason'" in capsys.readouterr().err
    assert run(monkeypatch, ["merge", "fail", mid,
                             "--reason", "the check needs a fixture"],
               DESK) == 0
    capsys.readouterr()
    record = merges_by_id()[mid]
    assert record["result"] == "failed"
    assert record["fail_reason"] == "the check needs a fixture"
    assert tasks_by_title("flow")["first"]["state"] == "built"
    assert git(repo, "rev-parse", "feat") == sha_before

    assert run(monkeypatch, ["merge", "take", mid], DESK) == 1
    assert "already failed" in capsys.readouterr().err
    assert run(monkeypatch, ["merge", "land", mid], DESK,
               cwd=repo) == 1
    assert "already failed" in capsys.readouterr().err


# --------------------------------------------------------------------------
# `task landed` belongs to the desk now
# --------------------------------------------------------------------------


def test_supervisor_task_landed_is_refused(env, monkeypatch, capsys):
    """A supervisor landing past the desk is told to request a merge."""
    repo, _origin = make_repo(env)
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"),
                session_record(DESK, "merge-desk"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    before = len(store.read_ledger(paths.front_tasks_path("flow")))
    assert run(monkeypatch, ["task", "landed", first,
                             "--head", "abc123"], SUP) == 1
    _, err = capsys.readouterr()
    assert "'flow'" in err
    assert "merge desk" in err
    assert "request a merge" in err
    assert tasks_by_title("flow")["first"]["state"] == "built"
    assert len(store.read_ledger(paths.front_tasks_path("flow"))) == before


def test_self_front_supervisor_lands_directly(env, monkeypatch, capsys):
    """Foreman's own fronts (`merge = "self"`) keep the supervisor path."""
    repo, _origin = make_repo(env)
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "self", merge="self"))]) \
        == 0
    capsys.readouterr()
    from foreman import fronts as fronts_module
    assert fronts_module.read_front_record("self")["merge"] == "self"
    seed_roster(session_record(SUP, "supervisor", "self"))
    build_task(monkeypatch, capsys, "self", "first")
    first = tasks_by_title("self")["first"]["id"]
    assert run(monkeypatch, ["task", "landed", first,
                             "--head", "abc123"], SUP) == 0
    capsys.readouterr()
    assert tasks_by_title("self")["first"]["state"] == "landed"


def test_front_add_merge_field(env, monkeypatch, capsys):
    """Anything but "self", "desk" or absent is a violation."""
    repo, _origin = make_repo(env)
    brief_dir = write_brief(repo, "bogus", merge="later")
    assert run(monkeypatch, ["front", "add", str(brief_dir)]) == 1
    _, err = capsys.readouterr()
    assert "'merge'" in err
    from foreman import fronts as fronts_module
    assert fronts_module.read_front_record("bogus") is None


# --------------------------------------------------------------------------
# Summoning the desk
# --------------------------------------------------------------------------


@pytest.fixture()
def fakes(env, monkeypatch):
    """A fake ``claude`` and a fake window launcher, both recording argv."""
    bindir = env / "bin"
    bindir.mkdir()
    record = env / "launcher-argv"
    vendor = env / "claude-argv"
    seen = env / "seen-at-entry"
    seen.mkdir()
    state = env / "state"

    launcher = bindir / "fake-window"
    launcher.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {record}\n'
        f'printf "AGENT_WS=%s\\n" "${{AGENT_WS-}}" >> {record}\n'
        'sid=${1#foreman-}\n'
        f'cp {state}/roster.json {seen}/roster.json 2>/dev/null\n'
        f'cp {state}/sessions/$sid/role-prompt.md {seen}/role-prompt.md '
        '2>/dev/null\n'
        f'cp {state}/sessions/$sid/vendor-session {seen}/vendor-session '
        '2>/dev/null\n'
        "shift\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    os.chmod(launcher, 0o755)

    claude = bindir / "claude"
    claude.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {vendor}\n',
        encoding="utf-8",
    )
    os.chmod(claude, 0o755)

    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FOREMAN_WINDOW_LAUNCHER", str(launcher))
    return {"launcher_argv": record, "claude_argv": vendor, "seen": seen,
            "bin": bindir}


def line(out: str, prefix: str) -> str:
    for row in out.splitlines():
        if row.startswith(prefix):
            return row.split(prefix, 1)[1].strip()
    raise AssertionError(f"no {prefix!r} line in output:\n{out}")


def roster() -> dict:
    return store.read_snapshot(paths.roster_path(),
                               default={"sessions": {}})["sessions"]


def test_dry_run_prompt_carries_the_queue_not_front_tasks(
        env, monkeypatch, capsys):
    """The desk's prompt names the queue; no front's tasks travel with it."""
    repo, _origin = make_repo(env)
    make_branch(repo, "feat", "feat.txt")
    write_check(env, "true")
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "flow"))]) == 0
    capsys.readouterr()
    seed_roster(session_record(SUP, "supervisor", "flow"))
    first = build_task(monkeypatch, capsys, "flow", "first")
    request_id(monkeypatch, capsys, "feat", "flow", [first],
               "main", cwd=repo)
    before = roster()

    rc = run(monkeypatch, ["launch", "merge-desk", "--workspace", "6",
                           "--repo", str(repo), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    sid = line(out, "session: ")
    prompt = Path(line(out, "role prompt: ")).read_text(encoding="utf-8")

    assert "{{" not in prompt and "}}" not in prompt
    assert "merge desk" in prompt
    assert sid in prompt
    # The queue travels: the waiting record with its front and branch.
    assert "feat -> main" in prompt
    assert "flow" in prompt
    # No front's tasks travel: the supervisor's sections are absent.
    assert "supervisor of front" not in prompt
    assert "WHAT: do the first thing." not in prompt
    # The desk's verbs, and none of the supervisor's planning verbs.
    assert "`foreman merge take" in prompt
    assert "`foreman merge land" in prompt
    assert "`foreman merge fail" in prompt
    assert "`foreman launch" not in prompt
    assert "`foreman job verify" not in prompt
    assert "does not ship them" not in prompt
    assert roster() == before


def test_launch_puts_the_desk_on_the_roster(env, fakes, monkeypatch, capsys):
    repo, _origin = make_repo(env)
    assert run(monkeypatch, ["launch", "merge-desk", "--workspace", "6",
                             "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    sid = line(out, "session: ")
    vendor_id = line(out, "vendor session: ")
    pid = int(line(out, "pid: "))

    record = roster()[sid]
    assert record["role"] == "merge-desk"
    assert record["pool"] == "opus"
    assert record["model"] == "claude-opus-5"
    assert record["front"] is None
    assert record["state"] == "running"
    assert pid != os.getpid()
    assert record["pid"] == pid
    assert procs.same_process(record["pid"], record["pid_starttime"])
    stored = (paths.session_dir(sid) / "vendor-session").read_text(
        encoding="utf-8").strip()
    assert stored == vendor_id

    at_entry = json.loads(
        (fakes["seen"] / "roster.json").read_text(encoding="utf-8"))
    assert at_entry["sessions"][sid]["state"] == "starting"
    assert "merge desk" in (
        fakes["seen"] / "role-prompt.md").read_text(encoding="utf-8")

    # One desk at a time: a second summon is refused by the first's name.
    capsys.readouterr()
    assert run(monkeypatch, ["launch", "merge-desk",
                             "--repo", str(repo), "--dry-run"]) == 1
    _, err = capsys.readouterr()
    assert sid in err


def test_a_front_that_declares_no_desk_lands_itself(env, monkeypatch,
                                                    capsys):
    """Absent means self, not desk.

    The default ran the other way, and a live front's supervisor was
    refused `task landed` with "request a merge instead" on a front whose
    brief never mentioned a desk — a true number missing from the screen
    because of a field nobody had written. The desk is a product swarm's
    discipline; a brief opts into it with `merge = "desk"`.
    """
    repo, _origin = make_repo(env)
    assert run(monkeypatch, ["front", "add",
                             str(write_brief(repo, "plain", merge=None))]) == 0
    capsys.readouterr()
    from foreman import fronts as fronts_module
    assert (fronts_module.read_front_record("plain") or {})["merge"] == ""
    seed_roster(session_record(SUP, "supervisor", "plain"))
    build_task(monkeypatch, capsys, "plain", "first")
    first = tasks_by_title("plain")["first"]["id"]
    assert run(monkeypatch, ["task", "landed", first,
                             "--head", "abc123"], SUP) == 0
    capsys.readouterr()
    assert tasks_by_title("plain")["first"]["state"] == "landed"
