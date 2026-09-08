"""Collector: one tick observes, derives, flags, relaunches.

Every test names the break it catches in its docstring. Clocks are
injected (tick(now=...)), processes are real short-lived Python
children, and the pool adapter is a scripted fake mirroring the real
observe structure exactly. No test sleeps more than a fraction of a
second; nothing waits on a threshold.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, paths, procs, store
from foreman.collector import (
    CollectorConfig,
    load_config,
    parse_duration,
    tick,
)
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter

MARKER = "foreman-test-vendor-probe"
NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def at(seconds_ago: float) -> str:
    return iso(datetime.fromtimestamp(NOW.timestamp() - seconds_ago,
                                      tz=timezone.utc))


class FakeAdapter(PoolAdapter):
    """Scripted pool: observe returns the script per session, else idle."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    script: dict[str, dict] = {}
    launched: list[LaunchContext] = []
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
        type(self).launched.append(ctx)
        return proc.pid

    def command_str(self, ctx: LaunchContext) -> str:
        return "fake-exec"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    return tmp_path


@pytest.fixture()
def fake_pool():
    from foreman import pools

    FakeAdapter.script = {}
    FakeAdapter.launched = []
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


def spawn(children, *argv: str) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, *argv])
    children.append(proc)
    return proc


def sleeper(children, *extra: str) -> subprocess.Popen:
    return spawn(children, "-c", "import time; time.sleep(30)", *extra)


def later(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def vendor_exe(tmp_path: Path, children, name: str) -> subprocess.Popen:
    """An unclaimed process whose executable basename is ``name``."""
    assert name.replace("-", "").replace("_", "").isalnum(), name
    proc = subprocess.Popen(["bash", "-c", f"exec -a {name} sleep 30"])
    children.append(proc)
    for _ in range(50):
        try:
            with open(f"/proc/{proc.pid}/cmdline", "rb") as handle:
                if handle.read().split(b"\0")[0].decode() == name:
                    break
        except OSError:
            pass
        time.sleep(0.02)
    return proc


def parent_with_child(children) -> subprocess.Popen:
    """A parent that spawns one child, both sleeping: a real two-pid tree."""
    return spawn(children, "-c",
                 "import subprocess, sys, time; "
                 "p = subprocess.Popen([sys.executable, '-c', "
                 "'import time; time.sleep(30)']); time.sleep(30)")


def write_config(text: str) -> None:
    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def base_config(**overrides) -> str:
    lines = ["[collector]",
             f"vendor_markers = [\"{MARKER}\"]"]
    for key, value in overrides.items():
        lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


def seed_roster(entries: dict[str, dict]) -> None:
    store.write_snapshot(paths.roster_path(), {"sessions": entries})


def worker_session(sid: str, pid: int | None, **fields) -> dict:
    base = {
        "id": sid, "role": "muse", "pool": "fake",
        "model": "fake-test-model", "component": "comp",
        "job": None, "pid": pid, "pgid": pid,
        "worktree": "", "log": "", "timeout": "",
        "launched_by": None, "started_at": iso(NOW),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }
    base.update(fields)
    return Session.from_dict(base).to_dict()


def open_anomalies() -> list[dict]:
    """Folded last-wins on (kind, subject), like every ledger reader:
    resolution appends a revised copy, so raw lines still show the old
    open copy."""
    folded: dict[tuple[str, str], dict] = {}
    for line in store.read_ledger(paths.anomalies_path()):
        if isinstance(line.get("kind"), str) and \
                isinstance(line.get("subject"), str):
            folded[(line["kind"], line["subject"])] = line
    return [line for line in folded.values()
            if line.get("resolved_at") is None]


def lines_for(kind: str, subject: str) -> list[dict]:
    return [line for line in store.read_ledger(paths.anomalies_path())
            if line.get("kind") == kind and line.get("subject") == subject]


def utime_tree(root, moment: float) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        os.utime(dirpath, (moment, moment))
        for name in filenames:
            os.utime(os.path.join(dirpath, name), (moment, moment))


def test_tick_records_roster_fields_and_derived_numbers(
        env, fake_pool, children):
    """A live session's cpu/observed-at land on the roster and every v0
    derived number lands in observed.json with hand-checked values."""
    write_config(base_config() + "[pools.fake]\nslots_total = 4\n")
    proc = sleeper(children)
    sid = "ses-live0001"
    FakeAdapter.script[sid] = {"transcript_mtime": NOW.timestamp() - 5,
                               "cpu_s": 4.5,
                               "finish_present": False,
                               "finish_rc": None}
    seed_roster({sid: worker_session(
        sid, proc.pid, started_at=at(60), last_declared_at=at(30),
        timeout="10m", job="job-1")})
    wt = env / "wt-one"
    (wt / "sub").mkdir(parents=True)
    (wt / "sub" / "artifact.txt").write_text("done\n", encoding="utf-8")
    utime_tree(wt, NOW.timestamp() - 120)
    store.append_ledger(paths.component_jobs_path("comp"), {
        "id": "job-1", "task": "tas-1", "kind": "implement", "role": "muse",
        "state": "running", "worktree": str(wt), "timeout": "10m",
        "started_at": at(120)})
    store.append_ledger(paths.slots_path(), {
        "pool": "fake", "component": "comp", "role": "muse", "job": "job-1",
        "session": sid, "granted_at": at(60), "released_at": None})
    store.append_ledger(paths.inbox_path(), {
        "id": "inb-1", "from": "ses-ask0001", "kind": "money",
        "question": "spend?", "recommendation": "yes",
        "asked_at": at(90)}, session_id="owner")

    payload = tick(now=NOW)

    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert set(roster) == {"sessions"}
    record = roster["sessions"][sid]
    assert set(record) == {f.name for f in dataclasses.fields(Session)}
    assert record["cpu_s"] == 4.5
    assert record["last_observed_at"] == iso(NOW)
    assert record["state"] == "running"

    session = payload["sessions"][sid]
    assert session["seconds_since_declared"] == 30.0
    # A first observation establishes the baseline and never accuses:
    # the session started a minute ago but activity is 0s old.
    assert session["seconds_since_activity"] == 0.0
    assert session["cpu_s"] == 4.5
    job = payload["jobs"]["job-1"]
    assert job["elapsed_s"] == 120.0
    assert job["timeout_s"] == 600.0
    assert job["minutes_since_write"] == 2.0
    assert payload["pools"] == {"fake": {"held": 1, "total": 4}}
    swarm = payload["swarm"]
    assert swarm["sessions_registered"] == 1
    assert swarm["sessions_observed"] == 1
    assert swarm["inbox_depth"] == 1
    assert swarm["inbox_oldest_s"] == 90.0
    assert swarm["open_anomalies"] == 0
    assert open_anomalies() == []


def test_once_verb_runs_a_tick(env, fake_pool, children, capsys):
    """`foreman collector once` exits 0 and leaves a fresh observed.json."""
    write_config(base_config())
    proc = sleeper(children)
    seed_roster({"ses-one0001": worker_session("ses-one0001", proc.pid)})
    assert cli.main(["collector", "once"]) == 0
    payload = json.loads(paths.observed_path().read_text(encoding="utf-8"))
    assert "at" in payload and "sessions" in payload
    assert "ses-one0001" in payload["sessions"]
    assert payload["swarm"]["sessions_registered"] == 1


def test_cpu_sums_the_child_tree(env, children):
    """The shell's own pid burns nothing: a sleeping parent with a busy
    child must still read busy. Single-pid accounting returns ~0 here."""
    from foreman.pools.muse import MuseAdapter

    parent = spawn(children, "-c",
                   "import subprocess, sys, time; "
                   "p = subprocess.Popen([sys.executable, '-c', "
                   "'while True: pass']); time.sleep(30)")
    grandchild: subprocess.Popen | None = None
    try:
        session = Session(id="ses-cpu0001", role="muse", pool="muse",
                          model=MuseAdapter.model, pid=parent.pid,
                          state="running")
        seen = 0.0
        for _ in range(15):
            seen = float(MuseAdapter().observe(session)["cpu_s"])
            if seen > 0.2:
                break
            time.sleep(0.1)
        assert seen > 0.2
    finally:
        procs.kill_tree(parent.pid)
        parent.wait()
        for proc in children:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert procs.tree_cpu_seconds(99999999) == 0.0
    assert MuseAdapter().observe(
        Session(id="ses-dead001", pid=99999999))["cpu_s"] == 0.0


def test_timeout_kill_really_kills(env, fake_pool, children):
    """An overdue running job's whole tree dies, the session reads killed,
    the job ledger reads failed, and the reason is on the anomaly line."""
    write_config(base_config())
    proc = parent_with_child(children)
    for _ in range(40):
        if len(procs.descendants(proc.pid)) > 1:
            break
        time.sleep(0.05)
    members = set(procs.descendants(proc.pid))
    assert len(members) > 1
    sid = "ses-time0001"
    seed_roster({sid: worker_session(
        sid, proc.pid, started_at=at(3600), timeout="30s",
        component="comp", job="job-9")})
    store.append_ledger(paths.component_jobs_path("comp"), {
        "id": "job-9", "state": "running", "started_at": at(3600),
        "timeout": "30s"})

    tick(now=NOW)

    assert proc.poll() is not None
    for member in members:
        assert not procs.pid_alive(member)
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "killed"
    (kind_lines := lines_for("job timeout", sid))
    assert len(kind_lines) == 1
    assert "3600" in kind_lines[0]["detail"]
    assert "30s" in kind_lines[0]["detail"]
    assert "killed" in kind_lines[0]["detail"]
    jobs = store.read_ledger(paths.component_jobs_path("comp"))
    assert jobs[-1]["id"] == "job-9" and jobs[-1]["state"] == "failed"
    tick(now=NOW)
    assert len(lines_for("job timeout", sid)) == 2
    assert lines_for("job timeout", sid)[-1]["resolved_at"] is not None


def test_supervisor_silent(env, fake_pool, children):
    """A supervisor quiet for 15 min with no running jobs is flagged; a
    running job for its component clears the line instead of doubling it."""
    write_config(base_config())
    sup = sleeper(children)
    sid = "ses-sup00001"
    seed_roster({sid: worker_session(
        sid, sup.pid, role="supervisor", pool="fake", component="comp-s",
        started_at=at(3600), last_declared_at=at(1200))})

    tick(now=NOW)
    silent = lines_for("supervisor silent", sid)
    assert len(silent) == 1
    assert silent[0]["resolved_at"] is None

    hand = sleeper(children)
    seed_roster({sid: worker_session(
        sid, sup.pid, role="supervisor", pool="fake", component="comp-s",
        started_at=at(3600), last_declared_at=at(1200)),
        "ses-hand0001": worker_session(
            "ses-hand0001", hand.pid, component="comp-s")})
    tick(now=NOW)
    assert len(lines_for("supervisor silent", sid)) == 2
    assert lines_for("supervisor silent", sid)[-1]["resolved_at"] == iso(NOW)


def test_silent_threshold_comes_from_config(env, fake_pool, children):
    """A 60 s configured silence fires at two minutes; the 15 min design
    default would stay quiet. A hard-coded call-site value fails this."""
    write_config(base_config(supervisor_silent_seconds=60))
    proc = sleeper(children)
    sid = "ses-sup00002"
    seed_roster({sid: worker_session(
        sid, proc.pid, role="supervisor", pool="fake",
        started_at=at(300), last_declared_at=at(120))})
    tick(now=NOW)
    assert len(lines_for("supervisor silent", sid)) == 1


def test_job_stalled(env, fake_pool, children):
    """No worktree change and no cpu change for ten minutes marks the
    session stalled and opens one line. The first tick only baselines."""
    write_config(base_config())
    proc = sleeper(children)
    sid = "ses-stal0001"
    wt = env / "wt-stalled-1"
    wt.mkdir(parents=True)
    (wt / "artifact.txt").write_text("half\n", encoding="utf-8")
    utime_tree(wt, NOW.timestamp() - 2000)
    FakeAdapter.script[sid] = {"transcript_mtime": None,
                               "cpu_s": 1.5,
                               "finish_present": False,
                               "finish_rc": None}
    seed_roster({sid: worker_session(
        sid, proc.pid, started_at=at(3600), worktree=str(wt))})

    tick(now=NOW)
    assert lines_for("job stalled", sid) == []

    tick(now=later(700))

    assert lines_for("job stalled", sid)[0]["resolved_at"] is None
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "stalled"


def test_stall_flap_appends_no_second_line(env, fake_pool, children):
    """Three ticks under one standing stall leave exactly one open line:
    baseline, then stalled, then still stalled with no duplicate."""
    write_config(base_config())
    proc = sleeper(children)
    sid = "ses-stal0002"
    wt = env / "wt-stalled-2"
    wt.mkdir(parents=True)
    (wt / "artifact.txt").write_text("half\n", encoding="utf-8")
    utime_tree(wt, NOW.timestamp() - 2000)
    FakeAdapter.script[sid] = {"transcript_mtime": None,
                               "cpu_s": 1.5,
                               "finish_present": False,
                               "finish_rc": None}
    seed_roster({sid: worker_session(
        sid, proc.pid, started_at=at(3600), worktree=str(wt))})
    tick(now=NOW)
    tick(now=later(700))
    assert len(lines_for("job stalled", sid)) == 1
    tick(now=later(710))
    assert len(lines_for("job stalled", sid)) == 1


def test_resolved_stall_closes_without_reopening(env, fake_pool, children):
    """Fresh worktree activity resolves the stall line; staying active
    afterwards appends no second open line."""
    write_config(base_config())
    proc = sleeper(children)
    sid = "ses-stal0003"
    wt = env / "wt-stalled-3"
    wt.mkdir(parents=True)
    (wt / "artifact.txt").write_text("half\n", encoding="utf-8")
    utime_tree(wt, NOW.timestamp() - 2000)
    FakeAdapter.script[sid] = {"transcript_mtime": None,
                               "cpu_s": 1.5,
                               "finish_present": False,
                               "finish_rc": None}
    seed_roster({sid: worker_session(
        sid, proc.pid, started_at=at(3600), worktree=str(wt))})
    tick(now=NOW)
    tick(now=later(700))
    assert len(lines_for("job stalled", sid)) == 1

    (wt / "artifact.txt").write_text("more\n", encoding="utf-8")
    moment = later(710).timestamp()
    os.utime(wt / "artifact.txt", (moment, moment))
    os.utime(wt, (moment, moment))
    tick(now=later(710))
    tick(now=later(720))
    resolved = lines_for("job stalled", sid)
    assert len(resolved) == 2
    assert resolved[0]["resolved_at"] is None
    assert resolved[1]["resolved_at"] == iso(later(710))
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "running"
    assert open_anomalies() == []


def test_job_tail(env, fake_pool, children):
    """A finish marker with living descendants is a tail, not a stall or
    an exit; once the tree is gone the line resolves and the job reads
    returned."""
    write_config(base_config())
    parent = spawn(children, "-c",
                   "import subprocess, sys, time; "
                   "p = subprocess.Popen([sys.executable, '-c', "
                   "'import time; time.sleep(30)']); time.sleep(30)")
    for _ in range(40):
        if len(procs.descendants(parent.pid)) > 1:
            break
        time.sleep(0.05)
    sid = "ses-tail0001"
    FakeAdapter.script[sid] = {"transcript_mtime": NOW.timestamp(),
                               "cpu_s": 9.0,
                               "finish_present": True,
                               "finish_rc": 0}
    seed_roster({sid: worker_session(sid, parent.pid, started_at=at(3600))})
    tick(now=NOW)
    tail = lines_for("job tail", sid)
    assert len(tail) == 1
    assert tail[0]["resolved_at"] is None
    assert lines_for("job stalled", sid) == []
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "running"

    procs.kill_tree(parent.pid)
    parent.wait()
    for proc in children:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    tick(now=NOW)
    assert lines_for("job tail", sid)[-1]["resolved_at"] == iso(NOW)
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "exited"


def test_intruder_then_resolved(env, fake_pool, children):
    """A process whose executable is the marker and no session claims is
    flagged by pid; a dead pid clears the line instead of lingering."""
    write_config(base_config())
    proc = vendor_exe(env, children, MARKER)
    seed_roster({})

    tick(now=NOW)
    found = lines_for("intruder", str(proc.pid))
    assert len(found) == 1
    assert MARKER in found[0]["detail"] or str(proc.pid) in found[0]["detail"]

    proc.kill()
    proc.wait()
    tick(now=NOW)
    assert lines_for("intruder", str(proc.pid))[-1]["resolved_at"] == iso(NOW)


def test_claimed_processes_are_not_intruders(env, fake_pool, children):
    """A roster session's own tree, marker as its executable or not, never
    reads as an intruder."""
    write_config(base_config())
    proc = vendor_exe(env, children, MARKER)
    sid = "ses-claim001"
    seed_roster({sid: worker_session(sid, proc.pid)})
    tick(now=NOW)
    assert lines_for("intruder", str(proc.pid)) == []


def test_unregistered_writer(env, fake_pool, children, monkeypatch, capsys):
    """A refused call from an unknown session goes through the caller
    refusal and opens one line; a repeated refusal appends no second open
    line; the owner's own lines never do."""
    write_config(base_config())
    seed_roster({})
    monkeypatch.setenv("FOREMAN_SESSION", "ses-ghost99")
    assert cli.main(["rule", "list"]) == 1
    assert "unregistered writer" in capsys.readouterr().err
    assert cli.main(["rule", "list"]) == 1
    capsys.readouterr()
    ghost = lines_for("unregistered writer", "ses-ghost99")
    assert len(ghost) == 1

    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    store.append_ledger(paths.inbox_path(), {
        "id": "inb-owner", "from": "owner", "kind": "money",
        "question": "q?", "recommendation": "r",
        "asked_at": at(10)}, session_id="owner")
    tick(now=NOW)
    assert lines_for("unregistered writer", "owner") == []
    assert len(lines_for("unregistered writer", "ses-ghost99")) == 1


def test_dead_supervisor_needs_a_person_not_a_spawn(
        env, fake_pool, children):
    """A dead supervisor is never auto-relaunched: no new session, no
    adapter call, slots released, and one open line saying a person must
    relaunch it from its checkpoint. A second tick adds no second line."""
    write_config(base_config())
    old = sleeper(children)
    old.kill()
    old.wait()
    sid = "ses-supdead1"
    seed_roster({sid: worker_session(
        sid, old.pid, role="supervisor", pool="fake", component="comp",
        started_at=at(300))})
    paths.session_dir(sid).mkdir(parents=True, exist_ok=True)
    paths.checkpoint_path(sid).write_text(
        json.dumps({"session": sid, "doing": "split the brief",
                    "next": "dispatch"}), encoding="utf-8")
    store.append_ledger(paths.slots_path(), {
        "pool": "fake", "component": "comp", "role": "supervisor",
        "job": None, "session": sid, "granted_at": at(300),
        "released_at": None})

    tick(now=NOW)
    tick(now=NOW)

    assert FakeAdapter.launched == []
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "exited"
    assert set(roster["sessions"]) == {sid}
    dead = lines_for("supervisor dead", sid)
    assert len(dead) == 1
    assert dead[0]["resolved_at"] is None
    assert "person" in dead[0]["detail"]
    assert "relaunch" in dead[0]["detail"]
    grants = store.read_ledger(paths.slots_path())
    assert grants[-1]["session"] == sid
    assert grants[-1]["released_at"] == iso(NOW)


def test_crash_loop_leaves_supervisor_dead(env, fake_pool, children):
    """A dead supervisor stays dead with one open line across ticks: the
    second tick must not resolve it and the adapter is never called."""
    write_config(base_config())
    old = sleeper(children)
    old.kill()
    old.wait()
    sid = "ses-supdead2"
    seed_roster({sid: worker_session(
        sid, old.pid, role="supervisor", pool="fake",
        started_at=at(3600))})
    store.write_snapshot(paths.collector_path(), {
        "sessions": {},
        "relaunches": [{"from": "ses-anc0001", "to": "ses-anc0002",
                        "at": at(1200)},
                       {"from": "ses-anc0002", "to": sid, "at": at(600)}],
        "parents": {"ses-anc0002": "ses-anc0001", sid: "ses-anc0002"}})

    tick(now=NOW)
    tick(now=NOW)

    assert FakeAdapter.launched == []
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert set(roster["sessions"]) == {sid}
    assert roster["sessions"][sid]["state"] == "exited"
    guard = lines_for("supervisor dead", sid)
    assert len(guard) == 1
    assert guard[0]["resolved_at"] is None
    assert "relaunch" in guard[0]["detail"]


def test_tick_without_proc_table_still_observes(env, fake_pool, children,
                                                monkeypatch):
    """With no /proc the tick falls back instead of crashing: a living
    session stays running and its adapter numbers still land."""
    write_config(base_config())
    monkeypatch.setattr(procs, "snapshot", lambda: {})
    proc = sleeper(children)
    sid = "ses-noproc01"
    FakeAdapter.script[sid] = {"transcript_mtime": NOW.timestamp(),
                               "cpu_s": 2.0,
                               "finish_present": False,
                               "finish_rc": None}
    seed_roster({sid: worker_session(sid, proc.pid)})

    payload = tick(now=NOW)

    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "running"
    assert roster["sessions"][sid]["cpu_s"] == 2.0
    assert payload["sessions"][sid]["alive"] is True


def test_unknown_pool_is_left_alone(env, fake_pool, children):
    """A session naming a pool with no adapter keeps its state across two
    ticks twenty minutes apart: without an observation source, unknown
    never reads as idle and never ages into a stall."""
    write_config(base_config())
    proc = sleeper(children)
    sid = "ses-nopool01"
    seed_roster({sid: worker_session(
        sid, proc.pid, pool="no-such-pool", started_at=at(3600))})

    payload = tick(now=NOW)
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "running"
    assert "cpu_s" not in payload["sessions"][sid]
    assert lines_for("job stalled", sid) == []

    payload = tick(now=later(1200))
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "running"
    assert "cpu_s" not in payload["sessions"][sid]
    assert "seconds_since_activity" not in payload["sessions"][sid]
    assert lines_for("job stalled", sid) == []


def repo_unit_template() -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    return os.path.join(root, "packaging", "foreman-collector.service")


def test_collector_unit_prints_the_packaging_template(env, capsys):
    """The verb prints the file under packaging/ with the placeholders
    filled: same sections, real state and config paths, nothing left as
    @NAME@, and no home directory baked into the committed file."""
    assert cli.main(["collector", "unit"]) == 0
    text = capsys.readouterr().out
    with open(repo_unit_template(), encoding="utf-8") as handle:
        template = handle.read()
    assert "@FOREMAN_BIN@" in template and "@STATE_DIR@" in template
    assert "/hom" + "e/" not in template
    binary = shutil.which("foreman")
    expected_bin = binary if binary else f"{sys.executable} -m foreman"
    expected = (template
                .replace("@FOREMAN_BIN@", expected_bin)
                .replace("@STATE_DIR@", str(paths.state_dir()))
                .replace("@CONFIG_DIR@", str(paths.config_dir())))
    assert text == expected
    assert "collector run" in text
    assert str(paths.state_dir()) in text
    assert "@FOREMAN_BIN@" not in text
    assert "@STATE_DIR@" not in text
    assert "@CONFIG_DIR@" not in text


def test_parse_duration_literals():
    """Timeout strings parse to hand-checked second counts."""
    assert parse_duration("20m") == 1200.0
    assert parse_duration("1h30m") == 5400.0
    assert parse_duration("45s") == 45.0
    assert parse_duration("2d") == 172800.0
    assert parse_duration("") is None
    assert parse_duration("soon") is None
    assert parse_duration("10") is None
    assert parse_duration(None) is None


def test_load_config_defaults_without_file(env):
    """No foreman.toml: every threshold is the design's value."""
    config = load_config()
    assert config.tick_seconds == 2
    assert config.supervisor_silent_seconds == 15 * 60
    assert config.job_stalled_seconds == 10 * 60
    assert config.relaunch_limit == 2
    assert config.relaunch_window_seconds == 60 * 60
    assert isinstance(load_config(), CollectorConfig)


def test_procs_state_field_and_zombie_dead():
    """The stat state is the letter after the comm, not the pgrp number;
    zombies read dead and never join a live tree."""
    info = procs._read_stat(os.getpid())
    assert info is not None
    _ppid, state, _cpu, _start = info
    assert state in ("R", "S", "D", "T", "t", "Z", "X", "x", "K", "W", "P")
    assert info[0] == os.getppid()
    table = {
        100: {"ppid": 0, "state": "S", "cpu_s": 0.0, "starttime": 1,
              "cmdline": "init"},
        101: {"ppid": 100, "state": "Z", "cpu_s": 0.0, "starttime": 2,
              "cmdline": "dead-child"},
        102: {"ppid": 100, "state": "S", "cpu_s": 0.0, "starttime": 3,
              "cmdline": "live-child"},
    }
    assert procs.descendants(100, table) == {100, 102}
    assert procs.descendants(101, table) == set()


def test_timeout_kill_uses_pgid_and_confirms(children):
    """kill_job signals the process group and reports no remainder: a
    child that shares the group dies with the parent."""
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time; "
         "p = subprocess.Popen([sys.executable, '-c', "
         "'import time; time.sleep(30)']); time.sleep(30)"],
        start_new_session=True)
    children.append(parent)
    for _ in range(40):
        if len(procs.descendants(parent.pid)) > 1:
            break
        time.sleep(0.05)
    assert len(procs.descendants(parent.pid)) > 1
    _signalled, remaining = procs.kill_job(parent.pid, parent.pid)
    assert remaining == set()
    assert parent.poll() is not None


def test_pid_reuse_is_not_alive_and_not_killed(env, fake_pool, children):
    """A roster pid whose starttime no longer matches is not the launched
    process: it reads dead and a timeout never signals the stranger."""
    write_config(base_config())
    proc = sleeper(children)
    sid = "ses-reuse001"
    real = procs.proc_starttime(proc.pid)
    assert real is not None
    FakeAdapter.script[sid] = {"transcript_mtime": None, "cpu_s": 0.0,
                               "finish_present": False, "finish_rc": None}
    seed_roster({sid: worker_session(
        sid, proc.pid, pid_starttime=real + 1, started_at=at(3600),
        timeout="30s")})

    payload = tick(now=NOW)

    assert payload["sessions"][sid]["alive"] is False
    assert proc.poll() is None
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "exited"
    assert lines_for("job timeout", sid) == []


def test_first_tick_never_accuses_old_job(env, fake_pool, children):
    """A job working happily for an hour is not stalled on tick one: the
    first observation baselines instead of backdating to launch."""
    write_config(base_config())
    proc = sleeper(children)
    sid = "ses-old00001"
    wt = env / "wt-old-job"
    wt.mkdir(parents=True)
    (wt / "out.txt").write_text("work\n", encoding="utf-8")
    FakeAdapter.script[sid] = {"transcript_mtime": None, "cpu_s": 3.0,
                               "finish_present": False, "finish_rc": None}
    seed_roster({sid: worker_session(
        sid, proc.pid, started_at=at(3600), worktree=str(wt))})

    payload = tick(now=NOW)

    assert lines_for("job stalled", sid) == []
    assert payload["sessions"][sid]["seconds_since_activity"] == 0.0


def test_stall_watches_worktree_not_transcript(env, fake_pool, children):
    """A job repeating itself in its log but writing no worktree file and
    burning no cpu still stalls: transcript chatter is not activity."""
    write_config(base_config())
    proc = sleeper(children)
    sid = "ses-logbusy1"
    wt = env / "wt-logbusy"
    wt.mkdir(parents=True)
    (wt / "artifact.txt").write_text("half\n", encoding="utf-8")
    utime_tree(wt, NOW.timestamp() - 2000)
    FakeAdapter.script[sid] = {"transcript_mtime": NOW.timestamp(),
                               "cpu_s": 1.5,
                               "finish_present": False, "finish_rc": None}
    seed_roster({sid: worker_session(
        sid, proc.pid, started_at=at(3600), worktree=str(wt))})

    tick(now=NOW)
    FakeAdapter.script[sid] = {"transcript_mtime": later(700).timestamp(),
                               "cpu_s": 1.5,
                               "finish_present": False, "finish_rc": None}
    tick(now=later(700))

    assert len(lines_for("job stalled", sid)) == 1


def test_overdue_job_dies_despite_finish_marker(env, fake_pool, children):
    """A finish marker never exempts a job from its timeout: an overdue
    tree is still killed and the job still reads failed."""
    write_config(base_config())
    proc = sleeper(children)
    sid = "ses-immort01"
    FakeAdapter.script[sid] = {"transcript_mtime": NOW.timestamp(),
                               "cpu_s": 0.0,
                               "finish_present": True, "finish_rc": 0}
    seed_roster({sid: worker_session(
        sid, proc.pid, started_at=at(3600), timeout="30s",
        component="comp", job="job-imm")})
    store.append_ledger(paths.component_jobs_path("comp"), {
        "id": "job-imm", "state": "running", "started_at": at(3600),
        "timeout": "30s"})

    tick(now=NOW)

    assert proc.poll() is not None
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["state"] == "killed"
    jobs = store.read_ledger(paths.component_jobs_path("comp"))
    assert jobs[-1]["id"] == "job-imm" and jobs[-1]["state"] == "failed"


def test_terminal_job_states_are_terminal(env, fake_pool, children):
    """A returned line is appended once and a verified line is never
    overwritten by a later tick."""
    write_config(base_config())
    proc = sleeper(children)
    sid = "ses-term0001"
    FakeAdapter.script[sid] = {"transcript_mtime": NOW.timestamp(),
                               "cpu_s": 0.0,
                               "finish_present": True, "finish_rc": 0}
    seed_roster({sid: worker_session(
        sid, proc.pid, component="comp", job="job-term")})
    store.append_ledger(paths.component_jobs_path("comp"), {
        "id": "job-term", "state": "running", "started_at": at(100)})

    proc.kill()
    proc.wait()
    tick(now=NOW)
    tick(now=NOW)
    returned = [line for line in
                store.read_ledger(paths.component_jobs_path("comp"))
                if line.get("id") == "job-term"]
    assert [line["state"] for line in returned] == ["running", "returned"]

    store.append_ledger(paths.component_jobs_path("comp"), dict(
        returned[-1], state="verified"))
    tick(now=NOW)
    final = [line for line in
             store.read_ledger(paths.component_jobs_path("comp"))
             if line.get("id") == "job-term"]
    assert final[-1]["state"] == "verified"


def test_slots_released_on_worker_death(env, fake_pool, children):
    """A dead worker's grant is released on death, not on a later
    relaunch: the pool stops showing it held."""
    write_config(base_config() + "[pools.fake]\nslots_total = 2\n")
    proc = sleeper(children)
    proc.kill()
    proc.wait()
    sid = "ses-slot0001"
    seed_roster({sid: worker_session(sid, proc.pid, component="comp")})
    store.append_ledger(paths.slots_path(), {
        "pool": "fake", "component": "comp", "role": "muse", "job": None,
        "session": sid, "granted_at": at(300), "released_at": None})

    tick(now=NOW)

    grants = store.read_ledger(paths.slots_path())
    assert grants[-1]["session"] == sid
    assert grants[-1]["released_at"] == iso(NOW)
    tick(now=NOW)
    assert len(store.read_ledger(paths.slots_path())) == len(grants)


def test_collector_refuses_workers(env, fake_pool, children, monkeypatch,
                                   capsys):
    """`foreman collector once` from a worker session is refused by name:
    a worker with a shell cannot run a tick to kill sessions."""
    write_config(base_config())
    proc = sleeper(children)
    seed_roster({"ses-wrk00001": worker_session("ses-wrk00001", proc.pid)})
    monkeypatch.setenv("FOREMAN_SESSION", "ses-wrk00001")
    assert cli.main(["collector", "once"]) == 1
    assert "may not call 'collector'" in capsys.readouterr().err
    assert not paths.observed_path().exists()


def test_intruder_matches_executable_and_all_pools(
        env, fake_pool, children):
    """Only the executable counts, and every shipped pool counts: an
    unclaimed `grok` binary is flagged while a python command merely
    mentioning one is not."""
    write_config("[collector]\nvendor_markers = [\"muse\", \"grok\", "
                 "\"claude\"]\n")
    grok = vendor_exe(env, children, "grok")
    mention = sleeper(children, "muse")
    seed_roster({})

    tick(now=NOW)

    assert len(lines_for("intruder", str(grok.pid))) == 1
    assert lines_for("intruder", str(mention.pid)) == []


def test_supervisor_silence_needs_running_jobs(env, fake_pool, children):
    """A live worker pid in a non-running session does not suppress
    supervisor silence: only running jobs count."""
    write_config(base_config(supervisor_silent_seconds=60))
    sup = sleeper(children)
    hand = sleeper(children)
    sid = "ses-sup00003"
    seed_roster({
        sid: worker_session(
            sid, sup.pid, role="supervisor", pool="fake",
            started_at=at(600), last_declared_at=at(120)),
        "ses-exited01": worker_session(
            "ses-exited01", hand.pid, component=None, state="exited"),
    })
    tick(now=NOW)
    assert len(lines_for("supervisor silent", sid)) == 1


def test_tail_ignores_output_draining():
    """The wrapper's `tee` flushing the log after the marker is not a
    retry; any other living descendant still is."""
    from foreman.collector import _tail_children
    table = {
        10: {"ppid": 0, "state": "S", "cpu_s": 0.0, "starttime": 1,
             "cmdline": "bash -c worker"},
        11: {"ppid": 10, "state": "S", "cpu_s": 0.0, "starttime": 2,
             "cmdline": "tee /tmp/ses/log"},
        12: {"ppid": 10, "state": "S", "cpu_s": 0.0, "starttime": 3,
             "cmdline": "python -c retry"},
    }
    assert _tail_children({10, 11}, 10, table) == set()
    assert _tail_children({10, 11, 12}, 10, table) == {12}
