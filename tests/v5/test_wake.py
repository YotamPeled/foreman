"""Wake events: one event per transition, heartbeats, the queue, tell.

Every test drives the real entry points against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``. The one test that runs a full
tick uses a scripted fake pool adapter and real short-lived Python
children that are killed before the tick: nothing here starts a real
vendor process. Clocks are injected where the code takes them.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, paths, procs, store
from foreman import wake as wake_module
from foreman.caller import SESSION_ENV
from foreman.collector import _mark_job, load_config, tick
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter

SUP = "ses-sup0001"
SUP_OTHER = "ses-sup0002"
FOREMAN_SES = "ses-for0001"
WORKER = "ses-wrk0001"
JOB = "job-0001"
FRONT = "alpha"
OTHER_FRONT = "beta"


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def ago(minutes: float) -> str:
    return iso(now() - timedelta(minutes=minutes))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    return tmp_path


class FakeAdapter(PoolAdapter):
    """Scripted pool: observe returns the script per session, else idle."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    script: dict[str, dict] = {}
    children: list[subprocess.Popen] = []

    def observe(self, session: Session) -> dict:
        return dict(self.script.get(session.id or "", {
            "transcript_mtime": None,
            "cpu_s": 0.0,
            "finish_present": False,
            "finish_rc": None,
        }))

    def launch(self, ctx: LaunchContext) -> int:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        type(self).children.append(proc)
        ctx.pid_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
        return proc.pid

    def command_str(self, ctx: LaunchContext) -> str:
        return "fake-exec"


@pytest.fixture()
def fake_pool():
    from foreman import pools

    FakeAdapter.script = {}
    FakeAdapter.children = []
    pools.register("fake", FakeAdapter())
    try:
        yield FakeAdapter
    finally:
        pools.unregister("fake")
        for proc in FakeAdapter.children:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


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


def sleeper(children, *argv: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", *argv])
    children.append(proc)
    return proc


def session(sid: str, role: str, **fields) -> dict:
    base = {
        "id": sid, "role": role, "pool": "fake",
        "model": "fake-test-model", "front": FRONT,
        "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "",
        "launched_by": None, "started_at": iso(now()),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }
    base.update(fields)
    return Session.from_dict(base).to_dict()


def seat(entries: dict[str, dict]) -> None:
    store.write_snapshot(paths.roster_path(), {"sessions": entries})


def running_job(job_id: str = JOB, task: str = "tas-0001") -> None:
    store.append_ledger(paths.front_jobs_path(FRONT), {
        "id": job_id, "task": task, "kind": "implement", "role": "muse",
        "state": "running", "started_at": ago(5)})


def events_for(sid: str) -> list[dict]:
    return wake_module.read_events(sid)


def run_as(monkeypatch, session_id: str | None) -> None:
    if session_id is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session_id)


def test_job_transition_emits_one_event_not_two(env):
    """A running -> failed transition wakes the launcher once however many
    ticks observe the terminal state afterwards."""
    seat({SUP: session(SUP, "supervisor"),
          WORKER: session(WORKER, "muse", job=JOB, launched_by=SUP)})
    running_job()
    stamp = iso(now())
    _mark_job(FRONT, JOB, "failed", None)
    _mark_job(FRONT, JOB, "failed", None)
    _mark_job(FRONT, JOB, "failed", stamp)
    folded = events_for(SUP)
    assert len(folded) == 1
    event = folded[0]
    assert event["id"].startswith("wke-")
    assert event["reason"] == "job failed"
    assert event["job"] == JOB
    assert event["front"] == FRONT
    assert event["task"] == "tas-0001"
    assert event["delivered_at"] is None
    raw = store.read_ledger(paths.session_events_path(SUP))
    assert len(raw) == 1


def test_timeout_wakes_timed_out_while_the_job_reads_failed(env):
    """The overdue kill marks the job failed but the cause is a timeout,
    and the launcher hears the cause, not the state."""
    seat({SUP: session(SUP, "supervisor"),
          WORKER: session(WORKER, "muse", job=JOB, launched_by=SUP)})
    running_job()
    _mark_job(FRONT, JOB, "failed", None, reason="job timed out")
    folded = events_for(SUP)
    assert [event["reason"] for event in folded] == ["job timed out"]


def test_owner_launched_job_wakes_nobody(env):
    """A job the owner launched names no session to wake: the owner
    watches the screen, not a ledger."""
    seat({WORKER: session(WORKER, "muse", job=JOB, launched_by=None)})
    running_job()
    _mark_job(FRONT, JOB, "returned", iso(now()))
    assert events_for(WORKER) == []


def test_finished_tick_wakes_the_launcher(env, fake_pool, children):
    """End to end through one tick with a fake session runner: the dead
    worker's finish marker returns the job and wakes its launcher."""
    proc = sleeper(children)
    sup_proc = sleeper(children)
    FakeAdapter.script[WORKER] = {"transcript_mtime": None,
                                  "cpu_s": 1.0,
                                  "finish_present": True, "finish_rc": 0}
    seat({SUP: session(SUP, "supervisor", pid=sup_proc.pid,
                       pgid=sup_proc.pid,
                       pid_starttime=procs.proc_starttime(sup_proc.pid),
                       last_declared_at=iso(now())),
          WORKER: session(WORKER, "muse", pid=proc.pid, pgid=proc.pid,
                          pid_starttime=procs.proc_starttime(proc.pid),
                          job=JOB, launched_by=SUP)})
    running_job()
    proc.kill()
    proc.wait()
    moment = now()
    tick(now=moment)
    tick(now=moment + timedelta(seconds=2))
    folded = events_for(SUP)
    assert [event["reason"] for event in folded] == ["job returned"]
    assert folded[0]["job"] == JOB


def test_heartbeat_only_in_the_absence_of_other_events(env):
    """An idle session hears a heartbeat; anything queued holds the next
    one back; a wake restarts the clock."""
    seat({SUP: session(SUP, "supervisor", started_at=ago(60)),
          FOREMAN_SES: session(FOREMAN_SES, "foreman",
                               started_at=iso(now()))})
    moment = now()
    assert wake_module.heartbeat_tick(moment, load_config()) == 1
    assert [event["reason"] for event in events_for(SUP)] == ["heartbeat"]
    assert events_for(FOREMAN_SES) == []
    # The unclaimed heartbeat is still waiting to wake it: no second one.
    assert wake_module.heartbeat_tick(moment, load_config()) == 0
    assert len(events_for(SUP)) == 1
    # Claimed, so the clock restarts: silence again, then a new heartbeat.
    # The foreman session has been idle just as long, so it hears one too.
    assert wake_module.claim_wake(SUP) is not None
    soon = moment + timedelta(minutes=5)
    assert wake_module.heartbeat_tick(soon, load_config()) == 0
    later = moment + timedelta(minutes=21)
    assert wake_module.heartbeat_tick(later, load_config()) == 2
    assert len(events_for(SUP)) == 2
    assert [event["reason"] for event in events_for(FOREMAN_SES)] == [
        "heartbeat"]
    # A claimed queue with another event waiting wakes on that event:
    # no heartbeat on top of it.
    assert wake_module.claim_wake(SUP, now=later) is not None
    wake_module.append_event(SUP, "told", later, text="one sentence")
    much_later = later + timedelta(minutes=60)
    assert wake_module.heartbeat_tick(much_later, load_config()) == 0
    assert [event["reason"] for event in events_for(SUP)][-1] == "told"


def test_heartbeat_counts_from_launch_with_no_wake_yet(env):
    """A session that never woke counts the interval from launch, and one
    launched moments ago hears nothing."""
    seat({SUP: session(SUP, "supervisor", started_at=ago(60)),
          SUP_OTHER: session(SUP_OTHER, "supervisor", front=OTHER_FRONT,
                             started_at=iso(now()))})
    wake_module.heartbeat_tick(now(), load_config())
    assert [event["reason"] for event in events_for(SUP)] == ["heartbeat"]
    assert events_for(SUP_OTHER) == []


def test_wake_carries_every_event_queued_during_a_turn(env):
    """Events queued while a turn runs wait; one wake after the turn ends
    carries them all, and the fold marks each delivered exactly once."""
    seat({SUP: session(SUP, "supervisor")})
    wake_module.turn_started(SUP)
    first = wake_module.append_event(SUP, "told", text="first")
    second = wake_module.append_event(SUP, "told", text="second")
    assert wake_module.claim_wake(SUP) is None
    wake_module.turn_ended(SUP)
    wake_wake = wake_module.claim_wake(SUP)
    assert wake_wake is not None
    assert [event["id"] for event in wake_wake] == [first["id"],
                                                    second["id"]]
    assert wake_module.claim_wake(SUP) == []
    folded = events_for(SUP)
    assert len(folded) == 2
    assert all(event["delivered_at"] is not None for event in folded)
    assert folded[0]["delivered_at"] == folded[1]["delivered_at"]


def test_claim_refused_while_a_turn_runs(env):
    """A claim during a turn is refused, not queued behind it."""
    seat({SUP: session(SUP, "supervisor")})
    wake_module.append_event(SUP, "told", text="waiting")
    wake_module.turn_started(SUP)
    try:
        assert wake_module.turn_running(SUP) is True
        assert wake_module.claim_wake(SUP) is None
        assert len(wake_module.pending_events(SUP)) == 1
    finally:
        wake_module.turn_ended(SUP)
    assert wake_module.claim_wake(SUP) is not None


def test_stale_turn_marker_clears_itself(env):
    """A turn marker older than the staleness never wedges the queue: the
    next claim treats it as gone and removes it."""
    seat({SUP: session(SUP, "supervisor")})
    wake_module.turn_started(SUP, now=now() - timedelta(hours=2))
    assert wake_module.turn_running(SUP) is False
    assert not paths.session_turn_path(SUP).exists()
    assert wake_module.claim_wake(SUP) == []


def test_tell_refused_for_supervisor_and_empty_text(env, monkeypatch,
                                                    capsys):
    """Only owner and foreman tell; an empty sentence is refused, never
    written."""
    seat({SUP: session(SUP, "supervisor"),
          FOREMAN_SES: session(FOREMAN_SES, "foreman")})
    run_as(monkeypatch, SUP)
    assert wake_module.tell_main(SUP, ["hello"]) == 1
    run_as(monkeypatch, None)
    assert wake_module.tell_main(SUP, []) == 1
    assert wake_module.tell_main(SUP, ["  "]) == 1
    assert events_for(SUP) == []
    assert wake_module.tell_main(SUP, ["one", "sentence"]) == 0
    run_as(monkeypatch, FOREMAN_SES)
    assert wake_module.tell_main(SUP, ["foreman", "says"]) == 0
    folded = events_for(SUP)
    assert [event["reason"] for event in folded] == ["told", "told"]
    assert folded[0]["text"] == "one sentence"
    assert folded[0]["from"] == "owner"
    assert folded[1]["from"] == FOREMAN_SES


def test_inbox_answer_wakes_the_asker(env, monkeypatch, capsys):
    """Answering files a ruling and wakes whoever asked, naming the inbox
    item and the ruling, not a prose blob."""
    from foreman import verbs

    seat({SUP_OTHER: session(SUP_OTHER, "supervisor", front=OTHER_FRONT),
          FOREMAN_SES: session(FOREMAN_SES, "foreman")})
    run_as(monkeypatch, SUP_OTHER)
    assert verbs.ask_main(["Spend", "more?"], "money", "yes", []) == 0
    capsys.readouterr()
    iid = verbs.read_inbox()[0][0]["id"]
    run_as(monkeypatch, FOREMAN_SES)
    assert verbs.answer_main(iid, ["yes"]) == 0
    out = capsys.readouterr().out
    rid = out.strip().split("ruling ")[1]
    folded = events_for(SUP_OTHER)
    answered = [event for event in folded
                if event["reason"] == "inbox answered"]
    assert len(answered) == 1
    assert answered[0]["inbox"] == iid
    assert answered[0]["ruling"] == rid


def test_rule_lands_on_its_fronts_supervisor(env, monkeypatch, capsys):
    """A front ruling wakes that front's supervisor naming rule and front;
    a swarm ruling wakes nobody."""
    from foreman import verbs

    seat({SUP: session(SUP, "supervisor"),
          SUP_OTHER: session(SUP_OTHER, "supervisor", front=OTHER_FRONT)})
    run_as(monkeypatch, None)
    assert verbs.rule_main(FRONT, ["keep", "going"]) == 0
    rid = capsys.readouterr().out.strip()
    folded = events_for(SUP)
    assert len(folded) == 1
    assert folded[0]["reason"] == "rule landed"
    assert folded[0]["rule"] == rid
    assert folded[0]["front"] == FRONT
    assert events_for(SUP_OTHER) == []
    assert verbs.rule_main("swarm", ["all", "fronts"]) == 0
    capsys.readouterr()
    assert events_for(SUP) == folded
    assert events_for(SUP_OTHER) == []


def test_wake_list_next_role_gates(env, monkeypatch, capsys):
    """A session reads and claims its own queue; another session is
    refused; the owner reads any."""
    seat({SUP: session(SUP, "supervisor"),
          SUP_OTHER: session(SUP_OTHER, "supervisor", front=OTHER_FRONT)})
    wake_module.append_event(SUP, "told", text="for you")
    run_as(monkeypatch, SUP_OTHER)
    assert wake_module.wake_list_main(SUP) == 1
    assert wake_module.wake_next_main(SUP) == 1
    assert len(wake_module.pending_events(SUP)) == 1
    run_as(monkeypatch, SUP)
    assert wake_module.wake_list_main(SUP) == 0
    assert "told" in capsys.readouterr().out
    assert wake_module.wake_next_main(SUP) == 0
    assert wake_module.pending_events(SUP) == []
    run_as(monkeypatch, None)
    assert wake_module.wake_list_main(SUP) == 0


def test_config_default_and_bad_value(env, tmp_path):
    """The heartbeat default is 20 minutes; a misshapen value falls back
    to it instead of crashing the daemon."""
    assert load_config().heartbeat_minutes == 20
    config_file = Path(str(tmp_path / "config" / "foreman.toml"))
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("[collector]\nheartbeat_minutes = \"often\"\n",
                           encoding="utf-8")
    assert load_config().heartbeat_minutes == 20
    config_file.write_text("[collector]\nheartbeat_minutes = -5\n",
                           encoding="utf-8")
    assert load_config().heartbeat_minutes == 20
    config_file.write_text("[collector]\nheartbeat_minutes = 5\n",
                           encoding="utf-8")
    assert load_config().heartbeat_minutes == 5


def test_wake_cli_parses(env, monkeypatch, capsys):
    """The `wake list` / `wake next` door works without importing."""
    seat({SUP: session(SUP, "supervisor")})
    wake_module.append_event(SUP, "told", text="knock knock")
    run_as(monkeypatch, None)
    assert cli.build_parser() is not None
    from foreman.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["wake", "list", SUP])
    assert args._handler(args) == 0
    assert "knock knock" in capsys.readouterr().out
    args = parser.parse_args(["wake", "next", SUP])
    assert args._handler(args) == 0
    assert wake_module.pending_events(SUP) == []
