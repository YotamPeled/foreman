"""The collector carries the wake: a tick spawns `foreman turn` detached.

Every test drives a real collector tick against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``, inheriting no ``FOREMAN_*``
variable. The systemd unit a carrier runs under is never executed here:
the tests substitute the tick's spawn through the same ``collector``
attribute production calls, with a double that refuses what the real
path refuses — anything but ``systemd-run --user
--unit=foreman-turn-<session>-<attempt> <absolute interpreter> -m
foreman turn <session>``, a session the roster never minted, or a child
environment carrying a calling session — and starts a real detached
``sleep`` in its place, so a stand-in still alive when the tick returns
proves the tick never waited on it.

The break each test catches: a queued wake no carrier carries, a second
carrier beside a running turn, a carrier for an empty queue, two unit
names alike (a corpse blocking the clock), a tick that waits, a held
wake stamped or reordered by a skip, a carrier born as its spawner, and
a ``foreman turn`` that does not really run the wake.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, paths, store
from foreman import collector as collector_module
from foreman import headless as headless_module
from foreman import wake as wake_module
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.entities import Session

SUP = "ses-sup0001"
SUP_OTHER = "ses-sup0002"
FOREMAN_SES = "ses-for0001"

VENDOR_ID = "11111111-2222-4333-8444-555555555555"
UNIT_RE = re.compile(r"foreman-turn-(.+)-([1-9][0-9]*)")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A whole machine's worth of Foreman state, inside tmp_path."""
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    return tmp_path


@pytest.fixture()
def turn_spawn(env, monkeypatch):
    """The tick's spawn, doubled at the attribute production calls.

    Refuses what the real path refuses: anything but a systemd-run
    carrier argv with the per-session per-attempt unit and the exact
    ``foreman turn <session>`` tail, a session the roster never minted,
    and a child world carrying a calling session. What passes starts a
    real detached sleep, which the teardown reaps.
    """
    calls: list[dict] = []
    children: list[subprocess.Popen] = []

    def double(argv: list[str], *, env: dict[str, str]):
        assert isinstance(argv, list) and len(argv) == 6, argv
        assert argv[0] == "systemd-run", argv
        assert argv[1] == "--user", argv
        assert argv[2].startswith("--unit="), argv
        match = UNIT_RE.fullmatch(argv[2].split("=", 1)[1])
        assert match is not None, argv
        sid, attempt = match.group(1), int(match.group(2))
        assert argv[3:] == [str(Path(sys.executable).resolve()),
                            "-m", "foreman", "turn", sid], argv
        assert isinstance(env, dict) and env, argv
        assert SESSION_ENV not in env, argv
        assert env.get("FOREMAN_STATE"), argv
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


@pytest.fixture()
def fake_claude(env, monkeypatch):
    """A fake `claude` on PATH: prints a result line, exits zero."""
    bindir = env / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        'echo "{\\"type\\":\\"system\\",\\"subtype\\":\\"init\\",'
        '\\"session_id\\":\\"$FAKE_VENDOR\\"}"\n'
        'echo "{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\",'
        '\\"is_error\\":false,\\"num_turns\\":3,'
        '\\"usage\\":{\\"input_tokens\\":10,\\"output_tokens\\":5}}"\n'
        'exit 0\n',
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_VENDOR", VENDOR_ID)
    return bindir


@pytest.fixture()
def fake_turn_spawn(fake_claude):
    """A `spawn` double for the turn itself: runs the script for real."""
    calls: list[list[str]] = []

    def spawn(argv: list[str], *, timeout_s: float):
        calls.append(list(argv))
        assert argv[0] == "systemd-run", argv
        child_env = dict(os.environ)
        child_env["PATH"] = str(fake_claude) + os.pathsep + os.environ["PATH"]
        return subprocess.run(
            ["bash", argv[-1]], capture_output=True, text=True,
            env=child_env, timeout=60)

    spawn.calls = calls  # type: ignore[attr-defined]
    return spawn


def session(sid: str, role: str, **fields) -> dict:
    base = {
        "id": sid, "role": role, "pool": "no-such-pool",
        "model": "fake-test-model", "front": "alpha",
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


def queue(sid: str, *texts: str) -> None:
    for text in texts:
        wake_module.append_event(sid, "told", text=text)


def pending(sid: str) -> list[dict]:
    return wake_module.pending_events(sid)


def run_as(monkeypatch, session_id: str | None) -> None:
    if session_id is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session_id)


def test_tick_spawns_one_carrier_for_a_queued_wake(env, turn_spawn):
    """A queued wake with no turn running goes out as one detached
    carrier, with the argv and per-attempt unit name the design says;
    the stand-in is still alive when the tick returns, so the tick
    never waited on it; and the spawn stamps nothing."""
    seat({SUP: session(SUP, "supervisor")})
    queue(SUP, "worker returned")
    tick(now=now())
    assert [(call["sid"], call["attempt"]) for call in turn_spawn] == \
        [(SUP, 1)]
    call = turn_spawn[0]
    assert call["unit"] == f"foreman-turn-{SUP}-1"
    assert call["argv"] == ["systemd-run", "--user",
                            f"--unit=foreman-turn-{SUP}-1",
                            str(Path(sys.executable).resolve()),
                            "-m", "foreman", "turn", SUP]
    assert [event["text"] for event in pending(SUP)] == ["worker returned"]


def test_tick_skips_a_session_already_running_a_turn(env, turn_spawn):
    """A fresh turn marker holds the carrier back, and the skip leaves
    the queued events unstamped and in order."""
    seat({SUP: session(SUP, "supervisor")})
    queue(SUP, "first", "second")
    wake_module.turn_started(SUP)
    tick(now=now())
    assert turn_spawn == []
    assert [event["text"] for event in pending(SUP)] == ["first", "second"]


def test_tick_skips_an_empty_queue(env, turn_spawn):
    """No queued wake, no carrier — even with no turn running."""
    seat({SUP: session(SUP, "supervisor"),
          SUP_OTHER: session(SUP_OTHER, "supervisor")})
    queue(SUP, "worker returned")
    tick(now=now())
    assert [call["sid"] for call in turn_spawn] == [SUP]


def test_two_ticks_while_the_turn_runs_carry_once(env, turn_spawn):
    """The first tick carries; the second, with the turn marker fresh
    as the running carrier left it, carries nothing further."""
    seat({SUP: session(SUP, "supervisor")})
    queue(SUP, "worker returned")
    tick(now=now())
    assert len(turn_spawn) == 1
    wake_module.turn_started(SUP)
    tick(now=now())
    assert len(turn_spawn) == 1


def test_second_carrier_uses_the_next_unit_name(env, turn_spawn):
    """Attempts never reuse a unit name: without a turn marker (the
    carrier died before its first turn) the next tick goes out as
    attempt two, so a failed carrier never blocks the next wake."""
    seat({SUP: session(SUP, "supervisor")})
    queue(SUP, "worker returned")
    tick(now=now())
    tick(now=now())
    assert [(call["sid"], call["attempt"], call["unit"])
            for call in turn_spawn] == [
        (SUP, 1, f"foreman-turn-{SUP}-1"),
        (SUP, 2, f"foreman-turn-{SUP}-2"),
    ]


def test_held_wake_goes_out_on_the_tick_after_the_turn(env, turn_spawn):
    """A wake queued mid-turn waits out the first tick untouched and
    goes out on the second, when the marker reads stale — still
    unstamped and in its original order at send time."""
    seat({SUP: session(SUP, "supervisor")})
    queue(SUP, "first", "second")
    wake_module.turn_started(SUP)
    tick(now=now())
    assert turn_spawn == []
    assert [event["text"] for event in pending(SUP)] == ["first", "second"]
    wake_module.turn_started(SUP, now=now() - timedelta(minutes=20))
    tick(now=now())
    assert [(call["sid"], call["attempt"]) for call in turn_spawn] == \
        [(SUP, 1)]
    assert [event["text"] for event in pending(SUP)] == ["first", "second"]


def test_carrier_env_leaves_the_calling_session_behind(env, turn_spawn,
                                                       monkeypatch):
    """The carrier arrives as the owner whatever the tick ran as: no
    calling session travels, while the state directory does."""
    seat({SUP: session(SUP, "supervisor"),
          FOREMAN_SES: session(FOREMAN_SES, "foreman")})
    queue(SUP, "worker returned")
    run_as(monkeypatch, FOREMAN_SES)
    tick(now=now())
    assert len(turn_spawn) == 1
    child = turn_spawn[0]["env"]
    assert SESSION_ENV not in child
    assert child["FOREMAN_STATE"] == os.environ["FOREMAN_STATE"]


def test_turn_verb_carries_the_wake_for_real(env, fake_turn_spawn,
                                             monkeypatch, capsys):
    """`foreman turn <session>` runs the queued wake as a real turn:
    the vendor conversation resumes, the events stamp delivered, and
    one wake turn lands on the ledger."""
    seat({SUP: session(SUP, "supervisor")})
    queue(SUP, "worker returned")
    (paths.session_dir(SUP) / "vendor-session").parent.mkdir(
        parents=True, exist_ok=True)
    (paths.session_dir(SUP) / "vendor-session").write_text(
        VENDOR_ID + "\n", encoding="utf-8")
    monkeypatch.setattr(headless_module, "_default_spawn", fake_turn_spawn)
    run_as(monkeypatch, None)
    assert cli.main(["turn", SUP]) == 0
    assert f"turn {SUP}: ok" in capsys.readouterr().out
    assert pending(SUP) == []
    turns = headless_module.read_turns(SUP)
    assert len(turns) == 1
    assert turns[0]["kind"] == "wake"


def test_turn_verb_refusals(env, monkeypatch, capsys):
    """No session, an unknown session and another session's queue are
    refused; an empty queue is a printed status, not a refusal."""
    seat({SUP: session(SUP, "supervisor"),
          SUP_OTHER: session(SUP_OTHER, "supervisor")})
    run_as(monkeypatch, None)
    assert headless_module.turn_main(None) == 1
    assert headless_module.turn_main("ses-nobody") == 1
    run_as(monkeypatch, SUP_OTHER)
    assert headless_module.turn_main(SUP) == 1
    run_as(monkeypatch, SUP)
    assert headless_module.turn_main(SUP) == 0
    assert f"turn {SUP}: no-wake" in capsys.readouterr().out
