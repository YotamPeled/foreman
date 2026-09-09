"""Attach, headless status fields and the wake-based silence anomaly.

Every test drives the real entry points against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``, inheriting no ``FOREMAN_*``
variable. No test opens a real window (``attach`` takes a ``spawn``
double) and none touches a systemd unit. Clocks are pinned where the
screen reads them and real where the collector ticks.

The break each test catches: an attach that parks the window anywhere but
the launcher's workspace, an attach that writes the roster, an empty
window for a session with no turns, a window line for a helper that
never opened one, a status line without the wake reason/turn count/last-turn
age, `turn running` on a stale marker or a status run that clears one, a
between-turns session flagged silent, and a woken-but-turnless session
missed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from foreman import attach as attach_module
from foreman import cli, paths, store
from foreman import headless as headless_module
from foreman import wake as wake_module
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.entities import Session
from foreman.status import NOW_ENV

PINNED_NOW = "2026-09-08T12:00:00+00:00"
SID = "ses-att0001"
FRONT = "alpha"


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
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    return tmp_path


def headless_session(sid: str = SID, **fields) -> dict:
    base = {
        "id": sid, "role": "supervisor", "pool": "opus",
        "model": "claude-opus-5", "front": FRONT,
        "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": str(paths.session_log_path(sid)),
        "timeout": "", "launched_by": None,
        "headless": True, "started_at": ago(5),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }
    base.update(fields)
    return Session.from_dict(base).to_dict()


def seat(entries: dict[str, dict]) -> None:
    store.write_snapshot(paths.roster_path(), {"sessions": entries})


def seat_front() -> None:
    (paths.front_dir(FRONT)).mkdir(parents=True, exist_ok=True)
    (paths.front_record_path(FRONT)).write_text(
        json.dumps({"id": "frn-test0001", "name": FRONT,
                    "state": "active", "want": "test want",
                    "supervisor": SID}) + "\n", encoding="utf-8")
    (paths.front_tasks_path(FRONT)).write_text(
        json.dumps({"id": "tas-test01", "title": "first work",
                    "state": "ready", "units_done": 0,
                    "units_total": 2}) + "\n", encoding="utf-8")


def write_turn(sid: str, at: str, turn_id: str = "trn-test0001",
               kind: str = "first") -> None:
    path = paths.session_turns_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": turn_id, "session": sid,
                                 "kind": kind, "at": at}) + "\n")


def write_event(sid: str, at: str, reason: str = "job failed",
                event_id: str = "wke-test0001") -> None:
    path = paths.session_events_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"id": event_id, "session": sid, "reason": reason, "at": at,
             "job": "job-0001", "front": FRONT, "task": "tas-test01",
             "delivered_at": at}) + "\n")


def run_status(monkeypatch, capsys, tmp_path) -> str:
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    assert cli.main(["status"]) == 0
    return capsys.readouterr().out


def open_silence(sid: str) -> list[dict]:
    """Open `supervisor silent` lines for ``sid``, folded last-wins: a
    resolution appends a revised copy, so the raw ledger still holds the
    open line beside its resolution."""
    try:
        records = store.read_ledger(paths.anomalies_path())
    except OSError:
        return []
    folded: dict[tuple[str, str], dict] = {}
    for record in records:
        kind, subject = record.get("kind"), record.get("subject")
        if isinstance(kind, str) and isinstance(subject, str):
            folded[(kind, subject)] = record
    return [record for (kind, subject), record in folded.items()
            if kind == "supervisor silent" and subject == sid
            and record.get("resolved_at") is None]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]):
        self.calls.append(list(argv))
        return None


HELPER_REFUSAL = (
    "launch-window: export BOXES_SESSION=<your session id> first")


def write_helper(path: Path, body: str) -> Path:
    """A fake window helper: no real window, just the exit the test names."""
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def spawn_helper(helper: Path):
    """The production spawn, with argv[0] swapped for the double."""
    procs: list[Any] = []

    def spawn(argv: list[str]):
        proc = attach_module._default_spawn([str(helper), *argv[1:]])
        procs.append(proc)
        return proc

    spawn.procs = procs  # type: ignore[attr-defined]
    return spawn


def reap_helpers(spawn) -> None:
    """Kill anything the still-running double left behind."""
    for proc in getattr(spawn, "procs", ()):
        if proc.poll() is not None:
            continue
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


# --------------------------------------------------------------------------
# `foreman attach`: a window on the launcher's workspace, and nothing else.
# --------------------------------------------------------------------------


def test_attach_opens_a_tail_window_on_the_launchers_workspace(env):
    """The window tails the turn log where the launcher looks.

    An attach that pinned AGENT_WS would park the look on the wrong
    workspace; one that ran anything but tail would be a console, which
    is out of scope by name.
    """
    seat({SID: headless_session()})
    write_turn(SID, ago(2))
    spawn = Recorder()

    assert attach_module.attach_main(SID, spawn=spawn) == 0

    [argv] = spawn.calls
    assert argv[0] == attach_module._common.window_launcher_or_default()
    assert argv[1] == f"foreman-attach-{SID}"
    assert argv[2] == "bash"
    assert not any(part.startswith("AGENT_WS=") for part in argv)
    script = Path(argv[3]).read_text(encoding="utf-8")
    assert f"tail -n 200 -f {paths.session_log_path(SID)}" in script


def test_attach_close_leaves_the_roster_byte_identical(env, capsys):
    """Opening the window records nothing: closing it cannot change state.

    An attach that rostered its window (a pid, a stamp, a line) would
    leave a byte difference here; the close is the owner shutting a
    terminal, which no test can press, so the open writing nothing is
    the guarantee.
    """
    seat({SID: headless_session()})
    write_turn(SID, ago(2))
    before = paths.roster_path().read_bytes()

    assert attach_module.attach_main(SID, spawn=Recorder()) == 0
    capsys.readouterr()

    assert paths.roster_path().read_bytes() == before


def test_attach_to_a_session_with_no_turns_is_refused_by_name(
        env, capsys):
    """No turns means no log: the refusal names the session, no window.

    The break is an empty tail window for a session that never ran —
    or a refusal that blames the log file instead of the session.
    """
    seat({SID: headless_session()})

    assert attach_module.attach_main(SID, spawn=Recorder()) == 1
    assert SID in capsys.readouterr().err


def test_attach_to_an_unknown_session_is_refused_by_name(env, capsys):
    """A typo is a refusal naming the id, never a window on a guess."""
    seat({SID: headless_session()})

    assert attach_module.attach_main("ses-nope000", spawn=Recorder()) == 1
    assert "ses-nope000" in capsys.readouterr().err


def test_attach_role_gate_refuses_a_worker(env, monkeypatch, capsys):
    """Workers never open windows: the gate holds for attach too.

    The owner and a supervisor may look; a worker asking for a window is
    refused the way a worker asking for `--window` is.
    """
    worker = "ses-wrk0001"
    seat({SID: headless_session(),
          worker: headless_session(worker, role="muse", front=None,
                                   headless=False)})
    write_turn(SID, ago(2))
    monkeypatch.setenv(SESSION_ENV, worker)

    assert attach_module.attach_main(SID, spawn=Recorder()) == 1
    assert "may not call 'attach'" in capsys.readouterr().err


def test_attach_cli_parses_to_a_session(env):
    """`foreman attach <session>` reaches the verb with the id intact."""
    parser = cli.build_parser()
    args = parser.parse_args(["attach", SID])
    assert args.session == SID


def test_attach_refuses_when_the_helper_exits_nonzero(env, capsys, tmp_path):
    """A helper that dies with a line on stderr never opened a window.

    The break is the observed lie: the helper printed
    `launch-window: export BOXES_SESSION=…` and exited 2, no window
    opened, and attach still printed the window line and returned 0.
    """
    helper = write_helper(
        tmp_path / "launch-window",
        f"echo {HELPER_REFUSAL!r} >&2\nexit 2\n",
    )
    seat({SID: headless_session()})
    write_turn(SID, ago(2))
    spawn = spawn_helper(helper)
    try:
        rc = attach_module.attach_main(SID, spawn=spawn)
        captured = capsys.readouterr()

        assert rc != 0
        assert HELPER_REFUSAL in captured.err
        assert "window:" not in captured.out
    finally:
        reap_helpers(spawn)


def test_attach_prints_the_window_when_the_helper_stays_running(
        env, capsys, tmp_path):
    """A helper still running after the grace opened the window.

    The test waits the grace once, not twice: a helper that sleeps far
    longer than the grace is enough to prove still-running, and sitting
    out the grace a second time would only prove the clock.
    """
    helper = write_helper(tmp_path / "launch-window", "exec sleep 30\n")
    seat({SID: headless_session()})
    write_turn(SID, ago(2))
    spawn = spawn_helper(helper)
    started = time.monotonic()
    try:
        rc = attach_module.attach_main(SID, spawn=spawn)
        elapsed = time.monotonic() - started
        captured = capsys.readouterr()

        assert rc == 0
        assert f"window: {attach_module.attach_window_name(SID)}" in captured.out
        # One second of grace, not two: a second wait would only prove the clock.
        assert elapsed < 2.0
    finally:
        reap_helpers(spawn)


def test_attach_treats_a_helper_that_exits_zero_as_success(
        env, capsys, tmp_path):
    """A launcher that hands over and returns still opened a window.

    The break is treating any exit as failure: the helper daemonized
    the terminal and left, which is how a successful open looks.
    """
    helper = write_helper(tmp_path / "launch-window", "exit 0\n")
    seat({SID: headless_session()})
    write_turn(SID, ago(2))
    spawn = spawn_helper(helper)
    try:
        rc = attach_module.attach_main(SID, spawn=spawn)
        captured = capsys.readouterr()

        assert rc == 0
        assert f"window: {attach_module.attach_window_name(SID)}" in captured.out
    finally:
        reap_helpers(spawn)


# --------------------------------------------------------------------------
# Status: the headless line carries wakes and turns, never a process.
# --------------------------------------------------------------------------


def test_status_line_carries_wake_reason_turns_and_last_turn_age(
        env, monkeypatch, capsys, tmp_path):
    """The three facts, hand-derived against the pinned clock.

    The event at 11:50 reads 10m, two turns read as 2, the last at 11:57
    reads 3m. A line built from the process table would show none of
    these for a session with no pid.
    """
    seat({SID: headless_session(started_at="2026-09-08T11:00:00+00:00")})
    seat_front()
    write_event(SID, "2026-09-08T11:50:00+00:00")
    write_turn(SID, "2026-09-08T11:55:00+00:00", turn_id="trn-test0001")
    write_turn(SID, "2026-09-08T11:57:00+00:00", turn_id="trn-test0002",
               kind="wake")

    out = run_status(monkeypatch, capsys, tmp_path)

    assert "last wake 'job failed' 10m ago" in out
    assert "2 turns" in out
    assert "last turn 3m ago" in out
    assert "turn running" not in out


def test_status_shows_turn_running_only_while_the_marker_is_fresh(
        env, monkeypatch, capsys, tmp_path):
    """Fresh marker (11:59) reads running; stale (11:00) does not.

    The default staleness is ten minutes, so these two markers sit one
    minute inside and fifty outside it. Either one misread inverts the
    only liveness signal a headless session has.
    """
    seat({SID: headless_session(started_at="2026-09-08T11:00:00+00:00")})
    seat_front()
    write_event(SID, "2026-09-08T11:50:00+00:00")
    write_turn(SID, "2026-09-08T11:55:00+00:00")
    marker = paths.session_turn_path(SID)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(
        {"session": SID,
         "started_at": "2026-09-08T11:59:00+00:00"}), encoding="utf-8")

    assert "turn running" in run_status(monkeypatch, capsys, tmp_path)

    marker.write_text(json.dumps(
        {"session": SID,
         "started_at": "2026-09-08T11:00:00+00:00"}), encoding="utf-8")
    assert "turn running" not in run_status(monkeypatch, capsys, tmp_path)


def test_status_never_clears_a_stale_turn_marker(
        env, monkeypatch, capsys, tmp_path):
    """Status reads; only the turn path clears. A status run that unlinked
    the marker would end the turn it claims is running."""
    seat({SID: headless_session(started_at="2026-09-08T11:00:00+00:00")})
    seat_front()
    marker = paths.session_turn_path(SID)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(
        {"session": SID,
         "started_at": "2026-09-08T11:00:00+00:00"}), encoding="utf-8")

    run_status(monkeypatch, capsys, tmp_path)

    assert marker.exists()


def test_status_names_an_unwoken_turnless_session_honestly(
        env, monkeypatch, capsys, tmp_path):
    """No wake and no turn is `no wake yet · 0 turns`, not a blank."""
    seat({SID: headless_session(started_at="2026-09-08T11:00:00+00:00")})
    seat_front()

    out = run_status(monkeypatch, capsys, tmp_path)

    assert "no wake yet" in out
    assert "0 turns" in out


def test_status_leaves_a_windowed_supervisor_line_alone(
        env, monkeypatch, capsys, tmp_path):
    """The golden lines keep their shape: no wake words without headless."""
    seat({SID: headless_session(started_at="2026-09-08T11:00:00+00:00",
                                headless=False)})
    seat_front()
    write_event(SID, "2026-09-08T11:50:00+00:00")
    write_turn(SID, "2026-09-08T11:55:00+00:00")

    out = run_status(monkeypatch, capsys, tmp_path)

    assert "last wake" not in out
    assert "turns" not in out.replace("tasks", "")


# --------------------------------------------------------------------------
# The anomaly: silence from wakes and turns, never window activity.
# --------------------------------------------------------------------------


def test_between_turns_with_an_empty_queue_is_neither_silent_nor_stalled(
        env):
    """The false alarm this task exists to prevent: a healthy headless
    session holds no pid and no window, and the tick must not read that
    as dead, silent or stalled."""
    seat({SID: headless_session()})
    headless_module.append_turn(SID, {"kind": "first"})

    tick(now=now())

    assert open_silence(SID) == []
    roster = store.read_snapshot(paths.roster_path(),
                                 default={"sessions": {}})
    assert roster["sessions"][SID]["state"] == "running"


def test_between_turns_with_a_queued_wake_is_not_silent(env):
    """Pending events with turns on the ledger is a session waiting for
    its next turn — working as designed, not a session that never
    started."""
    seat({SID: headless_session()})
    headless_module.append_turn(SID, {"kind": "first"})
    wake_module.append_event(SID, "heartbeat")

    tick(now=now())

    assert open_silence(SID) == []


def test_a_woken_session_that_ran_no_turn_is_silent(env):
    """Woken and turnless: the turn path never picked the wake up."""
    seat({SID: headless_session()})
    wake_module.append_event(SID, "told", text="knock knock")

    tick(now=now())

    [line] = open_silence(SID)
    assert "ran no turn" in line["detail"]
    assert "told" in line["detail"]


def test_a_turnless_session_quiet_past_the_threshold_is_silent(env):
    """No turn and no wake for longer than the silence window: launched
    half an hour ago against a fifteen-minute threshold."""
    seat({SID: headless_session(started_at=ago(30))})

    tick(now=now())

    [line] = open_silence(SID)
    assert "no wake" in line["detail"]


def test_a_fresh_turnless_session_is_not_silent_yet(env):
    """Launched moments ago with nothing queued: no clock, no accusation."""
    seat({SID: headless_session(started_at=iso(now()))})

    tick(now=now())

    assert open_silence(SID) == []


def test_silence_resolves_once_the_first_turn_lands(env):
    """The anomaly is open while turnless and closes on the first turn:
    the resolve pass runs the tick after the turn record appears."""
    seat({SID: headless_session()})
    wake_module.append_event(SID, "told", text="knock knock")
    tick(now=now())
    assert len(open_silence(SID)) == 1

    headless_module.append_turn(SID, {"kind": "first"})
    tick(now=now())

    assert open_silence(SID) == []


# --------------------------------------------------------------------------
# The new wake primitives status and the collector share.
# --------------------------------------------------------------------------


def test_last_wake_names_the_latest_event_by_stamp(env):
    """Two wakes an hour apart: the screen names the later reason."""
    seat({SID: headless_session()})
    first = wake_module.append_event(SID, "job failed", now=ago(60),
                                     job="job-0001")
    second = wake_module.append_event(SID, "heartbeat", now=ago(5))

    latest = wake_module.last_wake(SID)

    assert latest is not None
    assert latest["id"] == second["id"]
    assert latest["reason"] == "heartbeat"
    assert first["id"] != second["id"]


def test_last_wake_is_none_where_no_event_ever_named_the_session(env):
    """No ledger, no wake: the screen says `no wake yet`, not `?`."""
    seat({SID: headless_session()})

    assert wake_module.last_wake(SID) is None


def test_turn_fresh_agrees_with_turn_running_without_clearing(env):
    """Fresh reads fresh on both; stale reads stale on both and only the
    running half unlinks the marker."""
    seat({SID: headless_session()})
    wake_module.turn_started(SID, now=now())
    assert wake_module.turn_fresh(SID) is True
    assert wake_module.turn_running(SID) is True
    assert paths.session_turn_path(SID).exists()

    wake_module.turn_started(SID, now=now() - timedelta(minutes=30))
    assert wake_module.turn_fresh(SID) is False
    assert paths.session_turn_path(SID).exists()
    assert wake_module.turn_running(SID) is False
    assert not paths.session_turn_path(SID).exists()

    assert wake_module.turn_fresh(SID) is False
