"""The progress verbs: tasks move because a supervisor said so, with evidence.

Every test drives the real entry points against a fresh FOREMAN_STATE and
FOREMAN_CONFIG directory. The flow test goes through ``cli.main`` alone;
the collector step runs a real tick with a scripted pool adapter and a
real (then killed) process, the way ``tests/v0/test_collector.py`` does.
No test writes to a real state directory.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, paths, procs, store
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.pools import LaunchContext, PoolAdapter

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
OTHER_SUP = "ses-sup0002"
GHOST = "ses-ghost01"
WRK = "ses-wrk0001"

FLOW_BRIEF = """name      = "{name}"
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
scope = \"\"\"
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
\"\"\"
verify = "make check first"
size = 3
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
size = 2
after = ["first"]
"""


class FakeAdapter(PoolAdapter):
    """Scripted pool: observe returns the script per session, else idle."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "20m"
    interactive = False

    script: dict[str, dict] = {}

    def observe(self, session) -> dict:
        return dict(self.script.get(session.id or "", {
            "transcript_mtime": None,
            "cpu_s": 0.0,
            "finish_present": False,
            "finish_rc": None,
        }))

    def launch(self, ctx: LaunchContext) -> int:
        raise NotImplementedError

    def command_str(self, ctx: LaunchContext) -> str:
        return "fake-exec"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    return tmp_path


@pytest.fixture()
def fake_pool():
    from foreman import pools

    FakeAdapter.script = {}
    pools.register("fake", FakeAdapter())
    try:
        yield FakeAdapter
    finally:
        pools.unregister("fake")


@pytest.fixture()
def children():
    procs_list: list[subprocess.Popen] = []
    try:
        yield procs_list
    finally:
        for proc in procs_list:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def iso(moment: datetime) -> str:
    return moment.isoformat()


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def write_brief(root: Path, name: str) -> Path:
    """A brief in a throwaway repository that really has its land-on branch.

    `front add` refuses a land-on branch that does not exist, so a fixture
    brief needs a repository behind it; a directory with no repository is
    itself a refusal, which is the point of the check.
    """
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        FLOW_BRIEF.format(name=name), encoding="utf-8")
    if not (root / ".git").exists():
        for argv in (["init", "-q", "-b", "main"],
                     ["-c", "user.email=t@t", "-c", "user.name=t",
                      "commit", "-q", "--allow-empty", "-m", "root"]):
            subprocess.run(["git", "-C", str(root), *argv], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return brief_dir


def add_front(env, monkeypatch, capsys, name="flow"):
    assert run(monkeypatch, ["front", "add", str(write_brief(env, name))]) == 0
    capsys.readouterr()


def make_self_landing(front):
    """Flag a test front as one that lands itself, the way a brief with
    `merge = "self"` records it: a revised copy of the front line, the way
    every other front change is written."""
    from foreman import fronts

    record = fronts.read_front_record(front)
    assert record is not None
    store.append_ledger(paths.front_record_path(front),
                        dict(record, merge="self"))


def sup_session(sid, front, **fields):
    base = {
        "id": sid, "role": "supervisor", "pool": "opus", "model": "opus",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": iso(NOW - timedelta(minutes=30)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }
    base.update(fields)
    return entities.Session.from_dict(base).to_dict()


def tasks_by_title(front):
    return {task["title"]: task for task in store.fold_by_id(
        store.read_ledger(paths.front_tasks_path(front)))}


def folded_job(front, jid):
    return {job["id"]: job for job in store.fold_by_id(
        store.read_ledger(paths.front_jobs_path(front)))}[jid]


def status_out(monkeypatch, capsys):
    assert run(monkeypatch, ["status"]) == 0
    return capsys.readouterr().out


def seed_supervisor(front, sid=SUP):
    store.write_snapshot(paths.roster_path(),
                         {"sessions": {sid: sup_session(sid, front)}})


def seed_running_job(front, jid, task_id, units):
    """The launcher's record: a running job owning the task's units."""
    store.append_ledger(paths.front_jobs_path(front), {
        "id": jid, "task": task_id, "kind": "implement", "role": "muse",
        "priority": 1, "spec_path": "/tmp/specs/job.md", "session": WRK,
        "worktree": "", "branch": "foreman/job", "log": "", "timeout": "20m",
        "units": units, "attempt": 1, "state": "running",
        "planned_at": iso(NOW - timedelta(minutes=10)),
        "queued_at": iso(NOW - timedelta(minutes=9)),
        "started_at": iso(NOW - timedelta(minutes=8)),
        "returned_at": None, "verified_at": None,
        "artifact": "", "verdict_path": "",
    })


def return_via_collector(children, front, jid):
    """Kill a real worker with a finish marker scripted: the tick that
    follows marks the job returned, the way a finished worker returns."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    children.append(proc)
    FakeAdapter.script[WRK] = {"transcript_mtime": NOW.timestamp(),
                               "cpu_s": 1.0,
                               "finish_present": True, "finish_rc": 0}
    roster = store.read_snapshot(paths.roster_path())
    roster["sessions"][WRK] = entities.Session.from_dict({
        "id": WRK, "role": "muse", "pool": "fake", "model": "fake-test-model",
        "front": front, "job": jid, "pid": proc.pid, "pgid": proc.pid,
        "pid_starttime": procs.proc_starttime(proc.pid),
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": SUP, "started_at": iso(NOW - timedelta(minutes=8)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }).to_dict()
    store.write_snapshot(paths.roster_path(), roster)
    proc.kill()
    proc.wait()
    tick(now=NOW)


def test_flow_2_ready_to_landed(env, fake_pool, children,
                                monkeypatch, capsys):
    """Flow 2 through the command line: ready, running, returned,
    verified, built, landed — with `foreman status` naming each state
    as it changes and the successor releasing only on landing.

    On a `merge = "self"` front, where the supervisor still lands
    directly; every other front lands through a merge request the desk
    consumes (see tests/v2/test_merge_desk.py).
    """
    add_front(env, monkeypatch, capsys)
    make_self_landing("flow")
    tasks = tasks_by_title("flow")
    first, second = tasks["first"]["id"], tasks["second"]["id"]
    assert tasks["first"]["state"] == "ready"
    assert tasks["second"]["state"] == "waiting"
    seed_supervisor("flow")
    seed_running_job("flow", "job-flow1", first, [1, 2, 3])
    jobs_before = len(store.read_ledger(paths.front_jobs_path("flow")))
    tasks_before = len(store.read_ledger(paths.front_tasks_path("flow")))

    return_via_collector(children, "flow", "job-flow1")
    assert folded_job("flow", "job-flow1")["state"] == "returned"
    assert "returned" in status_out(monkeypatch, capsys)

    assert run(monkeypatch, ["job", "verify", "job-flow1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "all green"], SUP) == 0
    capsys.readouterr()
    job = folded_job("flow", "job-flow1")
    assert job["state"] == "verified"
    assert job["verified_at"] is not None
    assert tasks_by_title("flow")["first"]["units_done"] == 3
    evidence = store.read_ledger(paths.front_evidence_path("flow"))
    assert len(evidence) == 1
    assert evidence[0]["status"] == "CONFIRMED"
    assert evidence[0]["command"] == "make check first"
    assert evidence[0]["on"] == "job-flow1"
    out = status_out(monkeypatch, capsys)
    assert "verified" in out
    assert "first \u2014 ready \u00b7 units 3/3" in out

    assert run(monkeypatch, ["task", "built", first], SUP) == 0
    capsys.readouterr()
    assert tasks_by_title("flow")["first"]["state"] == "built"
    assert tasks_by_title("flow")["second"]["state"] == "waiting"
    out = status_out(monkeypatch, capsys)
    assert "first \u2014 built \u00b7 units 3/3" in out
    assert "second \u2014 waiting \u00b7 units 0/2" in out

    assert run(monkeypatch, ["task", "landed", first,
                             "--head", "abc123"], SUP) == 0
    capsys.readouterr()
    assert tasks_by_title("flow")["first"]["state"] == "landed"
    assert tasks_by_title("flow")["second"]["state"] == "ready"
    out = status_out(monkeypatch, capsys)
    assert "first \u2014 landed \u00b7 units 3/3 \u00b7 head abc123" in out
    assert "second \u2014 ready \u00b7 units 0/2" in out

    # Appends, not edits: the collector's return plus the verify grew the
    # job ledger, and the units update, built, landed and the successor's
    # release grew the task ledger; the folds carry the new values.
    assert len(store.read_ledger(
        paths.front_jobs_path("flow"))) == jobs_before + 2
    assert len(store.read_ledger(
        paths.front_tasks_path("flow"))) == tasks_before + 4
    assert tasks_by_title("flow")["first"]["head"] == "abc123"


def test_unknown_session_is_refused_by_name(env, monkeypatch, capsys):
    """A caller who is not on the roster is refused, naming the session."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    before = len(store.read_ledger(paths.front_tasks_path("flow")))
    assert run(monkeypatch, ["task", "built", first], GHOST) == 1
    _, err = capsys.readouterr()
    assert GHOST in err
    assert "unregistered writer" in err
    assert len(store.read_ledger(paths.front_tasks_path("flow"))) == before


def test_supervisor_of_another_front_names_both_fronts(
        env, monkeypatch, capsys):
    """The identity check can fail: a rostered session carrying another
    front's identity is refused, naming both fronts."""
    add_front(env, monkeypatch, capsys)
    store.write_snapshot(paths.roster_path(), {"sessions": {
        OTHER_SUP: sup_session(OTHER_SUP, "elsewhere")}})
    assert store.read_snapshot(
        paths.roster_path())["sessions"][OTHER_SUP]["front"] == "elsewhere"
    seed_running_job("flow", "job-flow1",
                     tasks_by_title("flow")["first"]["id"], [1])
    assert run(monkeypatch, ["job", "verify", "job-flow1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], OTHER_SUP) == 1
    _, err = capsys.readouterr()
    assert "elsewhere" in err
    assert "flow" in err


def test_built_short_names_both_numbers(env, monkeypatch, capsys):
    """`task built` with units short is refused, naming done and total."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    before = len(store.read_ledger(paths.front_tasks_path("flow")))
    assert run(monkeypatch, ["task", "built", "second"], SUP) == 1
    _, err = capsys.readouterr()
    assert "0/2" in err
    assert len(store.read_ledger(paths.front_tasks_path("flow"))) == before


def test_built_accepts_a_units_overshoot(env, monkeypatch, capsys):
    """A repair job carries its own unit, so a task that failed review and
    was repaired ends above its own count. That task is still buildable:
    only a short count means work is unaccounted for."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    store.append_ledger(paths.front_tasks_path("flow"),
                        dict(tasks_by_title("flow")["first"],
                             state="ready", units_done=2, units_total=1))
    assert run(monkeypatch, ["task", "built", "first"], SUP) == 0
    capsys.readouterr()
    assert tasks_by_title("flow")["first"]["state"] == "built"


def test_verify_not_returned_is_refused(env, monkeypatch, capsys):
    """A job that never came back cannot be verified."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    seed_running_job("flow", "job-flow1",
                     tasks_by_title("flow")["first"]["id"], [1, 2, 3])
    assert run(monkeypatch, ["job", "verify", "job-flow1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 1
    _, err = capsys.readouterr()
    assert "job-flow1" in err
    assert "not 'returned'" in err


def test_verify_confirmed_needs_command_and_output(
        env, fake_pool, children, monkeypatch, capsys):
    """A CONFIRMED claim with no command (or no output) behind it is not
    evidence, so the verify is refused and nothing is appended."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    seed_running_job("flow", "job-flow1",
                     tasks_by_title("flow")["first"]["id"], [1, 2, 3])
    return_via_collector(children, "flow", "job-flow1")
    jobs_before = len(store.read_ledger(paths.front_jobs_path("flow")))
    assert run(monkeypatch, ["job", "verify", "job-flow1",
                             "--confirmed",
                             "--output", "ok"], SUP) == 1
    assert "'--command'" in capsys.readouterr().err
    assert run(monkeypatch, ["job", "verify", "job-flow1",
                             "--confirmed",
                             "--command", "make check first"], SUP) == 1
    assert "'--output'" in capsys.readouterr().err
    assert len(store.read_ledger(paths.front_jobs_path("flow"))) == jobs_before
    assert store.read_ledger(paths.front_evidence_path("flow")) == []


def test_verify_twice_is_refused(env, fake_pool, children,
                                 monkeypatch, capsys):
    """The second verify of the same job is refused; the task's units are
    counted once, not twice."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    seed_running_job("flow", "job-flow1", first, [1, 2, 3])
    return_via_collector(children, "flow", "job-flow1")
    assert run(monkeypatch, ["job", "verify", "job-flow1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 0
    capsys.readouterr()
    jobs_before = len(store.read_ledger(paths.front_jobs_path("flow")))
    assert run(monkeypatch, ["job", "verify", "job-flow1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 1
    _, err = capsys.readouterr()
    assert "already verified" in err
    assert "job-flow1" in err
    assert len(store.read_ledger(paths.front_jobs_path("flow"))) == jobs_before
    assert tasks_by_title("flow")["first"]["units_done"] == 3


def test_landed_not_built_is_refused(env, monkeypatch, capsys):
    """Landing a task that was never built is refused by its state."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    before = len(store.read_ledger(paths.front_tasks_path("flow")))
    assert run(monkeypatch, ["task", "landed", "second",
                             "--head", "abc123"], SUP) == 1
    _, err = capsys.readouterr()
    assert "not 'built'" in err
    assert len(store.read_ledger(paths.front_tasks_path("flow"))) == before


def test_successor_releases_on_landing_not_before(
        env, monkeypatch, capsys):
    """A successor stays waiting while its predecessor is only built and
    becomes ready in the same call that lands it.

    On a `merge = "self"` front, where the supervisor still lands
    directly."""
    add_front(env, monkeypatch, capsys)
    make_self_landing("flow")
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    store.append_ledger(paths.front_jobs_path("flow"), {
        "id": "job-flow1", "task": first, "kind": "implement",
        "role": "muse", "priority": 1, "spec_path": "/tmp/specs/job.md",
        "session": None, "worktree": "", "branch": "", "log": "",
        "timeout": "20m", "units": [1, 2, 3], "attempt": 1,
        "state": "returned",
        "planned_at": iso(NOW - timedelta(minutes=10)),
        "queued_at": iso(NOW - timedelta(minutes=9)),
        "started_at": iso(NOW - timedelta(minutes=8)),
        "returned_at": iso(NOW - timedelta(minutes=2)),
        "verified_at": None, "artifact": "", "verdict_path": "",
    })
    assert run(monkeypatch, ["job", "verify", "job-flow1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["task", "built", first], SUP) == 0
    capsys.readouterr()
    assert tasks_by_title("flow")["second"]["state"] == "waiting"
    assert run(monkeypatch, ["task", "landed", first,
                             "--head", "deadbee"], SUP) == 0
    capsys.readouterr()
    assert tasks_by_title("flow")["second"]["state"] == "ready"


def test_job_fail_marks_failed_with_finding_on_task(
        env, fake_pool, children, monkeypatch, capsys):
    """`job fail` moves a returned job to failed and appends the finding
    to the task; a failed job can no longer be verified."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    seed_running_job("flow", "job-flow1", first, [1, 2, 3])
    return_via_collector(children, "flow", "job-flow1")
    findings_before = len(store.read_ledger(paths.front_findings_path("flow")))
    assert run(monkeypatch, ["job", "fail", "job-flow1",
                             "--finding",
                             "The draft never arrived"], SUP) == 0
    capsys.readouterr()
    assert folded_job("flow", "job-flow1")["state"] == "failed"
    findings = store.read_ledger(paths.front_findings_path("flow"))
    assert len(findings) == findings_before + 1
    assert findings[-1]["on"] == first
    assert findings[-1]["title"] == "The draft never arrived"
    # The finding a failed job files is named like any other, so the
    # supervisor can refer back to it without quoting its title.
    assert str(findings[-1]["id"]).startswith("fnd-")
    assert run(monkeypatch, ["job", "verify", "job-flow1",
                             "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 1
    assert "not 'returned'" in capsys.readouterr().err


def test_confirmed_evidence_without_command_is_refused(
        env, monkeypatch, capsys):
    """A free-standing CONFIRMED claim with no command is refused; a
    PLAUSIBLE one is written."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    assert run(monkeypatch, ["evidence", "--on", first,
                             "--claim", "The draft reads clean",
                             "--status", "CONFIRMED"], SUP) == 1
    _, err = capsys.readouterr()
    assert "'--command'" in err
    assert store.read_ledger(paths.front_evidence_path("flow")) == []
    assert run(monkeypatch, ["evidence", "--on", first,
                             "--claim", "The draft reads clean",
                             "--status", "PLAUSIBLE"], SUP) == 0
    capsys.readouterr()
    evidence = store.read_ledger(paths.front_evidence_path("flow"))
    assert len(evidence) == 1
    assert evidence[0]["on"] == first
    assert evidence[0]["status"] == "PLAUSIBLE"


def test_finding_appends_and_names_its_subject(
        env, monkeypatch, capsys):
    """`finding` lands on the front's ledger with the task id; a job is
    not a subject a finding can sit on."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    seed_running_job("flow", "job-flow1", first, [1])
    assert run(monkeypatch, ["finding", "--on", "second",
                             "--class", "scope",
                             "--title", "The second draft needs the first",
                             "--detail", "Nothing to label yet"], SUP) == 0
    capsys.readouterr()
    findings = store.read_ledger(paths.front_findings_path("flow"))
    assert len(findings) == 1
    assert findings[0]["on"] == tasks_by_title("flow")["second"]["id"]
    assert findings[0]["class"] == "scope"
    # append_ledger already stamps `by` from the session; that is the
    # filer, so a finding filed from outside is distinguishable without
    # a new field.
    assert findings[0]["by"] == SUP
    assert run(monkeypatch, ["finding", "--on", "job-flow1",
                             "--class", "scope",
                             "--title", "A title",
                             "--detail", "Some detail"], SUP) == 1
    _, err = capsys.readouterr()
    assert "job-flow1" in err
    assert len(store.read_ledger(paths.front_findings_path("flow"))) == 1


def test_finding_lands_with_id_and_prints_it(env, monkeypatch, capsys):
    """A finding filed through the verb is named with an fnd- id on the
    ledger and on stdout, so a supervisor can copy it."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    second = tasks_by_title("flow")["second"]["id"]
    assert run(monkeypatch, ["finding", "--on", "second",
                             "--class", "scope",
                             "--title", "The second draft needs the first",
                             "--detail", "Nothing to label yet"], SUP) == 0
    out = capsys.readouterr().out
    findings = store.read_ledger(paths.front_findings_path("flow"))
    assert len(findings) == 1
    fid = findings[0]["id"]
    assert isinstance(fid, str) and fid.startswith("fnd-")
    assert f"{fid} on {second}" in out


def test_two_findings_in_the_same_millisecond_get_different_ids(
        env, monkeypatch, capsys):
    """Two findings filed while the clock stands still still get distinct
    ids, so a supervisor can name each one."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    monkeypatch.setattr("foreman.ids.time.time_ns",
                        lambda: 1_725_000_000_000_000_000)
    assert run(monkeypatch, ["finding", "--on", "first",
                             "--class", "scope",
                             "--title", "The first draft is missing a label",
                             "--detail", "Nothing to file yet"], SUP) == 0
    first_out = capsys.readouterr().out
    assert run(monkeypatch, ["finding", "--on", "first",
                             "--class", "scope",
                             "--title", "The first draft also lacks a bound",
                             "--detail", "Still nothing to file"], SUP) == 0
    second_out = capsys.readouterr().out
    findings = store.read_ledger(paths.front_findings_path("flow"))
    ids = [row["id"] for row in findings]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(isinstance(fid, str) and fid.startswith("fnd-") for fid in ids)
    assert ids[0] in first_out
    assert ids[1] in second_out


def test_supervisor_of_another_front_files_a_finding(
        env, monkeypatch, capsys):
    """A finding moves nothing, so a supervisor rostered on another
    front may file one. It lands on the named front's ledger, and `by`
    is the filer's session — append_ledger already stamps that, so no
    extra field."""
    add_front(env, monkeypatch, capsys, name="flow")
    add_front(env, monkeypatch, capsys, name="harbor")
    store.write_snapshot(paths.roster_path(), {"sessions": {
        SUP: sup_session(SUP, "flow"),
        OTHER_SUP: sup_session(OTHER_SUP, "harbor")}})
    assert run(monkeypatch, ["finding", "--on", "flow",
                             "--class", "scope",
                             "--title", "The other front's draft is missing a bound",
                             "--detail", "Seen while preparing the next front"],
               OTHER_SUP) == 0
    capsys.readouterr()
    findings = store.read_ledger(paths.front_findings_path("flow"))
    assert len(findings) == 1
    assert findings[0]["on"] == "flow"
    assert findings[0]["by"] == OTHER_SUP
    assert store.read_ledger(paths.front_findings_path("harbor")) == []


def test_unknown_session_cannot_file_a_finding(env, monkeypatch, capsys):
    """A caller the roster does not know is refused in the same words
    as any other progress verb, and nothing is appended."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    before = store.read_ledger(paths.front_findings_path("flow"))
    assert run(monkeypatch, ["finding", "--on", "flow",
                             "--class", "scope",
                             "--title", "A title a ghost cannot file",
                             "--detail", "Nothing to record"], GHOST) == 1
    _, err = capsys.readouterr()
    assert f"unknown session '{GHOST}' (unregistered writer)" in err
    assert store.read_ledger(paths.front_findings_path("flow")) == before


def test_worker_may_not_file_a_finding(env, monkeypatch, capsys):
    """The verb opens to supervisors, not to everyone: a worker is
    refused by role and nothing is appended."""
    add_front(env, monkeypatch, capsys)
    store.write_snapshot(paths.roster_path(), {"sessions": {
        SUP: sup_session(SUP, "flow"),
        WRK: entities.Session.from_dict({
            "id": WRK, "role": "muse", "pool": "muse", "model": "muse",
            "front": "flow", "job": None, "pid": None,
            "launched_by": SUP,
            "started_at": iso(NOW - timedelta(minutes=5)),
            "state": "running"}).to_dict()}})
    before = store.read_ledger(paths.front_findings_path("flow"))
    assert run(monkeypatch, ["finding", "--on", "flow",
                             "--class", "scope",
                             "--title", "A title a worker cannot file",
                             "--detail", "Nothing to record"], WRK) == 1
    _, err = capsys.readouterr()
    assert "role 'muse' may not call 'finding'" in err
    assert store.read_ledger(paths.front_findings_path("flow")) == before


def test_worker_may_not_move_a_task(env, monkeypatch, capsys):
    """Progress verbs sit on the supervisor's row: a worker session is
    refused by role name and nothing is appended."""
    add_front(env, monkeypatch, capsys)
    store.write_snapshot(paths.roster_path(), {"sessions": {
        SUP: sup_session(SUP, "flow"),
        WRK: entities.Session.from_dict({
            "id": WRK, "role": "muse", "pool": "muse", "model": "muse",
            "front": "flow", "job": None, "pid": None,
            "launched_by": SUP,
            "started_at": iso(NOW - timedelta(minutes=5)),
            "state": "running"}).to_dict()}})
    before = len(store.read_ledger(paths.front_tasks_path("flow")))
    assert run(monkeypatch, ["task", "built", "second"], WRK) == 1
    _, err = capsys.readouterr()
    assert "'muse'" in err
    assert len(store.read_ledger(paths.front_tasks_path("flow"))) == before


def test_reset_puts_a_built_task_back_with_the_reason_on_the_screen(
        env, monkeypatch, capsys):
    """`task reset` is the inverse of the move that built a task.

    Written because a proof run marked a real front's first task built,
    units 1/1, with a fixture job, and the front that starts on that ledger
    next must not inherit it. The task goes back to the state it had before
    the work — ready here, because nothing precedes it — its units go back
    to zero, and the reason stays on `foreman status` until the task moves
    again.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    store.append_ledger(paths.front_jobs_path("flow"), {
        "id": "job-flow1", "task": first, "kind": "implement",
        "role": "muse", "priority": 1, "spec_path": "/tmp/specs/job.md",
        "session": None, "worktree": "", "branch": "", "log": "",
        "timeout": "20m", "units": [1, 2, 3], "attempt": 1,
        "state": "returned",
        "planned_at": iso(NOW - timedelta(minutes=10)),
        "queued_at": iso(NOW - timedelta(minutes=9)),
        "started_at": iso(NOW - timedelta(minutes=8)),
        "returned_at": iso(NOW - timedelta(minutes=2)),
        "verified_at": None, "artifact": "", "verdict_path": "",
    })
    assert run(monkeypatch, ["job", "verify", "job-flow1", "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 0
    assert run(monkeypatch, ["task", "built", first], SUP) == 0
    capsys.readouterr()
    assert tasks_by_title("flow")["first"]["state"] == "built"

    assert run(monkeypatch, ["task", "reset", first,
                             "--reason", "built by a fixture job"], SUP) == 0
    capsys.readouterr()
    back = tasks_by_title("flow")["first"]
    assert back["state"] == "ready"
    assert back["units_done"] == 0
    assert back["reset_reason"] == "built by a fixture job"
    assert "reset: built by a fixture job" in status_out(monkeypatch, capsys)


def test_reset_without_a_reason_is_refused(env, monkeypatch, capsys):
    """A task moving backwards without a reason tells the owner nothing."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    before = len(store.read_ledger(paths.front_tasks_path("flow")))
    assert run(monkeypatch, ["task", "reset", first], SUP) == 1
    assert "--reason" in capsys.readouterr().err
    assert len(store.read_ledger(paths.front_tasks_path("flow"))) == before


def test_reset_of_an_untouched_task_is_refused(env, monkeypatch, capsys):
    """Nothing to undo is a refusal, not a line on the ledger."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    before = len(store.read_ledger(paths.front_tasks_path("flow")))
    assert run(monkeypatch, ["task", "reset", first,
                             "--reason", "no work was done"], SUP) == 1
    assert "already 'ready'" in capsys.readouterr().err
    assert len(store.read_ledger(paths.front_tasks_path("flow"))) == before


def test_a_worker_may_not_reset_a_task(env, monkeypatch, capsys):
    """The same gate as every other progress verb."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    roster = store.read_snapshot(paths.roster_path())
    roster["sessions"][WRK] = sup_session(WRK, "flow", role="muse",
                                          pool="fake")
    store.write_snapshot(paths.roster_path(), roster)
    first = tasks_by_title("flow")["first"]["id"]
    assert run(monkeypatch, ["task", "reset", first,
                             "--reason", "because"], WRK) == 1
    assert "muse" in capsys.readouterr().err


def test_moving_forward_clears_the_reset_note(env, monkeypatch, capsys):
    """A reset shows only while it is the last thing that happened."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    store.append_ledger(paths.front_tasks_path("flow"),
                        dict(tasks_by_title("flow")["first"],
                             state="ready", units_done=0,
                             reset_reason="built by a fixture job",
                             reset_by="owner", reset_at=iso(NOW)))
    store.append_ledger(paths.front_jobs_path("flow"), {
        "id": "job-flow2", "task": first, "kind": "implement",
        "role": "muse", "priority": 1, "spec_path": "/tmp/specs/job.md",
        "session": None, "worktree": "", "branch": "", "log": "",
        "timeout": "20m", "units": [1, 2, 3], "attempt": 1,
        "state": "returned",
        "planned_at": iso(NOW - timedelta(minutes=10)),
        "queued_at": iso(NOW - timedelta(minutes=9)),
        "started_at": iso(NOW - timedelta(minutes=8)),
        "returned_at": iso(NOW - timedelta(minutes=2)),
        "verified_at": None, "artifact": "", "verdict_path": "",
    })
    assert run(monkeypatch, ["job", "verify", "job-flow2", "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 0
    capsys.readouterr()
    assert "reset_reason" not in tasks_by_title("flow")["first"]
    assert "reset:" not in status_out(monkeypatch, capsys)


def test_a_job_launched_by_task_title_is_listed_once(
        env, monkeypatch, capsys):
    """One job, one line, under the task it belongs to.

    `foreman launch --task` takes an id or a title while every reader links
    a job to its task by id, so a job recorded with the title hung under
    nothing and was drawn a second time by the line for jobs whose task no
    ledger names. The screen resolves a title on the way in.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    seed_running_job("flow", "job-title1", "first", [1])
    lines = status_out(monkeypatch, capsys).splitlines()
    assert sum(1 for line in lines if "muse job running" in line) == 1
    # The fallback draws a bare title line of its own before the job; the
    # task's own line always carries its state and units.
    assert [line for line in lines if line.strip() == "first"] == []
    where = next(i for i, line in enumerate(lines)
                 if line.strip().startswith("first \u2014 ready"))
    assert "muse job running" in lines[where + 1]


def test_verify_records_the_spec_it_verified(env, monkeypatch, capsys):
    """Evidence names the spec the verified job was launched from.

    A verification is a claim about a particular piece of work. Without the
    spec on the evidence, a probe job's verification and the front's own
    work read identically on the ledger — which is how a real front's first
    task came to be built by a fixture job.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    store.append_ledger(paths.front_jobs_path("flow"), {
        "id": "job-spec1", "task": first, "kind": "implement",
        "role": "muse", "priority": 1, "spec_path": "/tmp/specs/probe.md",
        "session": None, "worktree": "", "branch": "", "log": "",
        "timeout": "20m", "units": [1], "attempt": 1, "state": "returned",
        "planned_at": iso(NOW - timedelta(minutes=10)),
        "queued_at": iso(NOW - timedelta(minutes=9)),
        "started_at": iso(NOW - timedelta(minutes=8)),
        "returned_at": iso(NOW - timedelta(minutes=2)),
        "verified_at": None, "artifact": "", "verdict_path": "",
    })
    assert run(monkeypatch, ["job", "verify", "job-spec1", "--confirmed",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 0
    assert "/tmp/specs/probe.md" in capsys.readouterr().out
    evidence = store.read_ledger(paths.front_evidence_path("flow"))
    assert evidence[-1]["spec_path"] == "/tmp/specs/probe.md"


def test_a_fixture_front_is_marked_wherever_it_appears(
        env, monkeypatch, capsys):
    """A front for trying the runtime out is never mistaken for a real one.

    The runtime v1 proof ran its probe jobs against the panel front and
    left its first task built, units 1/1, by a job that wrote a PROBE.txt
    line. A proof needs a front of its own that says what it is.
    """
    write_brief(env, "trial")
    assert run(monkeypatch, ["front", "add", str(env / "trial"),
                             "--fixture"]) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["front", "list"]) == 0
    assert "trial · fixture" in capsys.readouterr().out
    assert "(fixture)" in status_out(monkeypatch, capsys)

    add_front(env, monkeypatch, capsys, name="real")
    out = status_out(monkeypatch, capsys)
    assert "real —" in out
    assert "real (fixture)" not in out


def test_a_job_that_names_no_task_can_still_be_verified(
        env, monkeypatch, capsys):
    """Front work outside the brief's tasks is closable.

    `foreman launch --front <f>` takes no task, and work commissioned
    outside a brief arrives that way. `job verify` had no task to add units
    to and refused with "names unknown task ''", so the job stayed
    `returned` on the screen for good — a lie about work that was merged.
    It verifies now, adding its units to nothing, because there is nothing
    for them to be units of. A job naming a task the ledger does not have
    is still refused.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    store.append_ledger(paths.front_jobs_path("flow"), {
        "id": "job-notask", "task": "", "kind": "implement",
        "role": "muse", "priority": 1, "spec_path": "/tmp/specs/extra.md",
        "session": None, "worktree": "", "branch": "", "log": "",
        "timeout": "20m", "units": [], "attempt": 1, "state": "returned",
        "planned_at": iso(NOW - timedelta(minutes=10)),
        "queued_at": iso(NOW - timedelta(minutes=9)),
        "started_at": iso(NOW - timedelta(minutes=8)),
        "returned_at": iso(NOW - timedelta(minutes=2)),
        "verified_at": None, "artifact": "", "verdict_path": "",
    })
    assert run(monkeypatch, ["job", "verify", "job-notask", "--confirmed",
                             "--command", "make check", "--output",
                             "ok"], SUP) == 0
    assert "no task" in capsys.readouterr().out
    assert folded_job("flow", "job-notask")["state"] == "verified"

    store.append_ledger(paths.front_jobs_path("flow"), {
        "id": "job-ghost", "task": "a task nobody wrote", "kind": "implement",
        "role": "muse", "priority": 1, "spec_path": "", "session": None,
        "worktree": "", "branch": "", "log": "", "timeout": "20m",
        "units": [], "attempt": 1, "state": "returned",
        "planned_at": iso(NOW), "queued_at": iso(NOW), "started_at": iso(NOW),
        "returned_at": iso(NOW), "verified_at": None,
        "artifact": "", "verdict_path": "",
    })
    assert run(monkeypatch, ["job", "verify", "job-ghost", "--confirmed",
                             "--command", "make check", "--output",
                             "ok"], SUP) == 1
    assert "unknown task" in capsys.readouterr().err


def test_a_supervisor_accounts_for_units_it_did_itself(
        env, monkeypatch, capsys):
    """Some work has no worker to dispatch it to.

    A front's proof is run by its supervisor, by the brief, so no job
    carries its units — and without a way to say so the runtime could
    never close the task its own brief assigns to the supervisor: no job,
    no units, no `built`, for work that was finished. It is not a way
    around the evidence: CONFIRMED evidence on the front is required
    first, and what the supervisor says it did travels onto the screen.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]

    assert run(monkeypatch, ["task", "built", first,
                             "--did-myself", "I ran the done-when"],
               SUP) == 1
    assert "CONFIRMED evidence" in capsys.readouterr().err

    assert run(monkeypatch, ["evidence", "--on", "flow",
                             "--claim", "the done-when ran",
                             "--status", "CONFIRMED",
                             "--command", "make check first",
                             "--output", "ok"], SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["task", "built", first,
                             "--did-myself", "I ran the done-when"],
               SUP) == 0
    assert "by hand: I ran the done-when" in capsys.readouterr().out
    built = tasks_by_title("flow")["first"]
    assert built["state"] == "built"
    assert built["units_done"] == built["units_total"]
    assert "by hand: I ran the done-when" in status_out(monkeypatch, capsys)


def test_units_still_have_to_be_accounted_for_without_the_flag(
        env, monkeypatch, capsys):
    """The flag is the only way past the count, and it is deliberate."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("flow")
    first = tasks_by_title("flow")["first"]["id"]
    assert run(monkeypatch, ["task", "built", first], SUP) == 1
    assert "every unit must be accounted for" in capsys.readouterr().err
