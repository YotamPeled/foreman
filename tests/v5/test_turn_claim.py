"""A headless turn's vendor process is the session's own, not an intruder.

Every test drives a collector tick against a fake process table and a
fresh ``FOREMAN_STATE``. None of them start a real vendor or a systemd
unit. The break each test catches is in its docstring.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from foreman import paths, procs, store
from foreman import headless as headless_module
from foreman import wake as wake_module
from foreman.caller import SESSION_ENV
from foreman.collector import _turn_tree, tick
from foreman.entities import Session
from foreman.pools import _common

SUP = "ses-sup0001"
NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

#: The wrapper the turn records (the shell) and the vendor it spawned.
WRAPPER = 481000
VENDOR = 481679
START = 100
VENDOR_CMD = (
    "claude -p --resume 11111111-2222-4333-8444-555555555555 "
    "Act on these wake events"
)


def iso(moment: datetime) -> str:
    return moment.isoformat()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    return tmp_path


def session(sid: str, **fields) -> dict:
    base = {
        "id": sid, "role": "supervisor", "pool": "no-such-pool",
        "model": "fake-test-model", "front": "alpha",
        "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "",
        "launched_by": None, "started_at": iso(NOW),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running", "headless": True,
    }
    base.update(fields)
    return Session.from_dict(base).to_dict()


def seat(entries: dict[str, dict]) -> None:
    store.write_snapshot(paths.roster_path(), {"sessions": entries})


def proc(pid: int, *, ppid: int = 1, starttime: int = START,
         cmdline: str, state: str = "S") -> dict:
    return {
        "ppid": ppid, "state": state, "cpu_s": 1.0,
        "starttime": starttime, "cmdline": cmdline,
    }


def mid_turn_table() -> dict[int, dict]:
    """Wrapper shell plus the vendor it started, inside territory."""
    return {
        WRAPPER: proc(WRAPPER, cmdline="bash /tmp/run.sh"),
        VENDOR: proc(VENDOR, ppid=WRAPPER, starttime=START + 1,
                     cmdline=VENDOR_CMD),
    }


def drive_table(monkeypatch, table: dict[int, dict], cwd: str) -> None:
    """The tick's process table and cwd lookups, never this machine."""
    monkeypatch.setattr(procs, "snapshot", lambda: dict(table))
    monkeypatch.setattr(
        procs, "working_directory",
        lambda pid, _cwd=cwd: _cwd if pid in table else None,
    )


def lines_for(kind: str, subject: str) -> list[dict]:
    try:
        records = store.read_ledger(paths.anomalies_path())
    except OSError:
        return []
    return [line for line in records
            if line.get("kind") == kind and line.get("subject") == subject]


def test_a_turn_owns_its_vendor_process(env, monkeypatch):
    """A tick with a session mid-turn, its vendor alive in a Foreman
    worktree and recorded by the turn, raises no intruder anomaly — not
    for the recorded shell, and not for the vendor that is its child."""
    worktree = env / "wt"
    worktree.mkdir()
    seat({SUP: session(SUP, worktree=str(worktree))})
    wake_module.turn_started(
        SUP, now=NOW, pid=WRAPPER, pid_starttime=START)
    drive_table(monkeypatch, mid_turn_table(), str(worktree))

    tick(now=NOW)

    assert lines_for("intruder", str(WRAPPER)) == []
    assert lines_for("intruder", str(VENDOR)) == []


def test_an_unclaimed_vendor_in_territory_is_still_an_intruder(
        env, monkeypatch):
    """The same process with no turn recorded is still flagged: the check
    did not simply stop looking."""
    worktree = env / "wt"
    worktree.mkdir()
    seat({SUP: session(SUP, worktree=str(worktree))})
    drive_table(monkeypatch, mid_turn_table(), str(worktree))

    tick(now=NOW)

    found = lines_for("intruder", str(VENDOR))
    assert len(found) == 1
    assert found[0]["resolved_at"] is None
    assert str(VENDOR) in found[0]["detail"]


def test_a_dead_turn_pid_claims_nothing(env, monkeypatch):
    """A turn marker naming a pid that is not alive claims nothing: a
    live vendor in the same worktree is still an intruder."""
    worktree = env / "wt"
    worktree.mkdir()
    seat({SUP: session(SUP, worktree=str(worktree))})
    wake_module.turn_started(
        SUP, now=NOW, pid=99999, pid_starttime=START)
    drive_table(monkeypatch, mid_turn_table(), str(worktree))

    tick(now=NOW)

    assert len(lines_for("intruder", str(VENDOR))) == 1


def test_a_recycled_turn_pid_claims_nothing(env, monkeypatch):
    """A turn marker whose starttime does not match the process now at
    that pid claims nothing: pid reuse is not the turn's tree."""
    worktree = env / "wt"
    worktree.mkdir()
    seat({SUP: session(SUP, worktree=str(worktree))})
    wake_module.turn_started(
        SUP, now=NOW, pid=WRAPPER, pid_starttime=START)
    table = mid_turn_table()
    table[WRAPPER] = proc(WRAPPER, starttime=START + 50,
                          cmdline="bash /tmp/run.sh")
    drive_table(monkeypatch, table, str(worktree))

    tick(now=NOW)

    assert len(lines_for("intruder", str(VENDOR))) == 1


def test_ending_a_turn_clears_the_claim(env, monkeypatch):
    """A turn that ends clears the claim, so a later unrelated process
    on that pid is flagged."""
    worktree = env / "wt"
    worktree.mkdir()
    seat({SUP: session(SUP, worktree=str(worktree))})
    wake_module.turn_started(
        SUP, now=NOW, pid=WRAPPER, pid_starttime=START)
    drive_table(monkeypatch, mid_turn_table(), str(worktree))

    tick(now=NOW)
    assert lines_for("intruder", str(VENDOR)) == []

    wake_module.turn_ended(SUP)
    tick(now=NOW)
    found = lines_for("intruder", str(VENDOR))
    assert len(found) == 1
    assert found[0]["resolved_at"] is None


def test_the_turn_script_records_the_process_on_the_marker(env):
    """The collector can only claim a turn it can see: the script the
    runner writes stamps this shell's pid and starttime on the marker
    before the vendor starts."""
    sid = "ses-turn0001"
    paths.session_dir(sid).mkdir(parents=True, exist_ok=True)
    wake_module.turn_started(sid, now=NOW)
    inner = headless_module.turn_script_inner(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        session_id=sid)
    script = paths.session_dir(sid) / "run.sh"
    _common.write_worker_script(script, inner)
    child = subprocess.Popen(["bash", str(script)])
    try:
        marker = None
        for _ in range(100):
            marker = store.read_snapshot(
                paths.session_turn_path(sid), default=None)
            if isinstance(marker, dict) and marker.get("pid"):
                break
            time.sleep(0.02)
        assert isinstance(marker, dict)
        assert marker["pid"] == child.pid
        assert marker["pid_starttime"] == procs.proc_starttime(child.pid)
    finally:
        child.kill()
        child.wait()
        wake_module.turn_ended(sid)


def test_a_stamp_keeps_the_turn_s_own_start_time(env):
    """The stamp fills in a pid; it does not restart the turn. Resetting
    ``started_at`` would make an old turn look fresh, and the clock reads
    that stamp to decide whether a turn is still running."""
    wake_module.turn_started(SUP, now=NOW)
    before = store.read_snapshot(paths.session_turn_path(SUP), default=None)
    assert isinstance(before, dict)
    wake_module.record_turn_pid(SUP, WRAPPER, pid_starttime=START)
    after = store.read_snapshot(paths.session_turn_path(SUP), default=None)
    assert isinstance(after, dict)
    assert after["started_at"] == before["started_at"]
    assert after["pid"] == WRAPPER


def test_a_stamp_before_the_marker_exists_still_claims(env):
    """A stamp that lands before the marker exists still claims: the
    shell that runs it is the turn, so a missing marker is a race, not
    an absence. Without the write, ``_turn_tree`` would return empty
    and the collector would treat the turn's own process as unclaimed."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        wake_module.record_turn_pid(SUP, child.pid)
        tree = _turn_tree(SUP, procs.snapshot())
        assert child.pid in tree
    finally:
        child.kill()
        child.wait()
        wake_module.turn_ended(SUP)


def test_turn_started_keeps_a_stamped_pid_and_a_new_turn_starts_clean(env):
    """``turn_started`` for the same turn does not drop a pid already
    stamped; a ``turn_started`` that begins a new turn starts clean.
    Overwriting the marker without the pid is how a whole turn's vendor
    looks like an intruder."""
    wake_module.record_turn_pid(SUP, WRAPPER, pid_starttime=START)
    wake_module.turn_started(SUP, now=NOW)
    marker = store.read_snapshot(paths.session_turn_path(SUP), default=None)
    assert isinstance(marker, dict)
    assert marker["pid"] == WRAPPER
    assert marker["pid_starttime"] == START

    wake_module.turn_ended(SUP)
    wake_module.turn_started(SUP, now=NOW)
    marker = store.read_snapshot(paths.session_turn_path(SUP), default=None)
    assert isinstance(marker, dict)
    assert "pid" not in marker
    assert "pid_starttime" not in marker


def test_a_tick_with_the_stamp_landed_files_no_intruder(env, monkeypatch):
    """A tick taken while a turn is running, with the stamp landed
    before ``turn_started``, files no intruder anomaly for the turn's
    own vendor. The ledger is the proof, not the claim set: a claim
    that never reaches the anomaly pass would still look green here
    if we only inspected trees."""
    worktree = env / "wt"
    worktree.mkdir()
    seat({SUP: session(SUP, worktree=str(worktree))})
    wake_module.record_turn_pid(SUP, WRAPPER, pid_starttime=START)
    wake_module.turn_started(SUP, now=NOW)
    drive_table(monkeypatch, mid_turn_table(), str(worktree))

    tick(now=NOW)

    assert lines_for("intruder", str(WRAPPER)) == []
    assert lines_for("intruder", str(VENDOR)) == []
