"""`foreman kill`, and a relaunch that summons instead of resuming.

Every test drives the real entry points against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``. Nothing here starts a real
vendor: a fake ``claude`` and a fake window launcher sit in a tmp ``bin``
and record their argv, and a worker is a ``sleep`` this test spawns so a
kill has a real process to end without ever aiming at the test runner.

The break each test catches: a kill that leaves the roster saying running
while the process is gone, a kill of a session the caller has no authority
over, a kill that silently keeps the slot, a relaunch that resumes a
vendor session and so reaches nobody, a relaunch that mints a second
Foreman session id and orphans the checkpoint and slots of the first, and
a collector that flags a dead supervisor for a person instead of summoning
it anew.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman import capacity, cli, collector, paths, procs, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter
from foreman.pools import _common

ROOT = Path(__file__).resolve().parents[2]
PANEL_BRIEF = ROOT / "briefs" / "panel"

SPEC_OK = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v0 -q\n"
    "Do not touch src/foreman/store.py.\n"
)

#: Every process a test started, waited on when the test ends: a kill
#: leaves a zombie until somebody reaps it, and nothing this suite spawns
#: outlives it.
_SPAWNED: list[subprocess.Popen] = []


class SleeperAdapter(PoolAdapter):
    """A pool whose worker is a real, disposable process in its own group.

    The v0 fake adapter writes the test runner's own pid, which is exactly
    the process a kill test must never aim at. This spawns a sleep under
    ``start_new_session`` so the recorded pid and process group are a real
    tree the launcher started and ``foreman kill`` can end.
    """

    name = "sleeper"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    def launch(self, ctx: LaunchContext) -> int:
        proc = subprocess.Popen(
            ["sleep", "600"], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        _SPAWNED.append(proc)
        ctx.pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
        return proc.pid

    def observe(self, session: Session) -> dict:
        return {"transcript_mtime": None, "cpu_s": 0.0,
                "finish_present": False}

    def command_str(self, ctx: LaunchContext) -> str:
        return f"sleeper --worktree {ctx.worktree}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(launch_module.VENDOR_CONFIG_ENV,
                       str(tmp_path / "home" / "vendor.json"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    # A windowed launch names its workspace since the launches task, and
    # the collector's own relaunch is a windowed launch: without a default
    # in the config it is refused by name, which is that task working.
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "foreman.toml").write_text(
        '[launch]\ndefault_workspace = "6"\n', encoding="utf-8")
    # A window must live long enough for the launcher to confirm it, and
    # not a second longer than this test needs.
    monkeypatch.setattr(launch_module, "WINDOW_LINGER_SECONDS", 5)
    yield tmp_path
    _cleanup(tmp_path)


def _cleanup(root: Path) -> None:
    """Nothing this test started outlives it: windows first, then sleeps."""
    roster = store.read_snapshot(root / "state" / "roster.json",
                                 default={"sessions": {}})
    for record in (roster.get("sessions") or {}).values():
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        if pid is None or pid == os.getpid():
            continue
        if not procs.same_process(pid, record.get("pid_starttime")):
            continue
        procs.kill_job(pid, record.get("pgid"))
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass
    while _SPAWNED:
        proc = _SPAWNED.pop()
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


@pytest.fixture()
def sleeper():
    from foreman import pools

    pools.register("sleeper", SleeperAdapter())
    try:
        yield
    finally:
        pools.unregister("sleeper")


@pytest.fixture()
def fakes(env, monkeypatch):
    """A fake ``claude`` and a fake window launcher, both recording argv."""
    bindir = env / "bin"
    bindir.mkdir()
    launcher_argv = env / "launcher-argv"
    vendor_argv = env / "claude-argv"

    launcher = bindir / "fake-window"
    launcher.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {launcher_argv}\n'
        "shift\n"
        'exec "$@"\n',
        encoding="utf-8")
    os.chmod(launcher, 0o755)

    claude = bindir / "claude"
    claude.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {vendor_argv}\n',
        encoding="utf-8")
    os.chmod(claude, 0o755)

    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FOREMAN_WINDOW_LAUNCHER", str(launcher))
    return {"launcher_argv": launcher_argv, "claude_argv": vendor_argv}


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "test")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("add", "seed.txt")
    run("commit", "-qm", "seed")
    return path


def line(out: str, prefix: str) -> str:
    for row in out.splitlines():
        if row.startswith(prefix):
            return row.split(prefix, 1)[1].strip()
    raise AssertionError(f"no {prefix!r} line in output:\n{out}")


def roster() -> dict:
    return store.read_snapshot(paths.roster_path(),
                               default={"sessions": {}})["sessions"]


def jobs(front: str) -> list[dict]:
    return store.fold_by_id(store.read_ledger(paths.front_jobs_path(front)))


def add_panel_front() -> None:
    assert cli.main(["front", "add", str(PANEL_BRIEF)]) == 0


def start_worker(env, capsys, *, front: str = "panel") -> tuple[str, str]:
    """One launched worker on a job. Returns (session id, job id)."""
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    rc = cli.main(["launch", "muse", "sleeper", str(spec),
                   "--repo", str(repo), "--worktree", str(env / "wt-one"),
                   "--front", front, "--task", "plugin skeleton and data feed",
                   "--units", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    sid = line(out, "session: ")
    return sid, roster()[sid]["job"]


def summon(env, capsys, repo: Path) -> str:
    assert cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                     "--repo", str(repo)]) == 0
    return line(capsys.readouterr().out, "session: ")


# --------------------------------------------------------------------------
# kill
# --------------------------------------------------------------------------


def test_kill_of_a_running_job_ends_it_and_says_who_and_why(
        env, sleeper, capsys):
    """The process ends, the records say killed, and the slot comes back.

    A kill that stopped the process but left the roster running would put
    a dead job on the screen as work in progress and hold its slot against
    the pool for as long as anyone looked.
    """
    add_panel_front()
    capsys.readouterr()
    sid, job_id = start_worker(env, capsys)
    pid = roster()[sid]["pid"]
    assert procs.same_process(pid, roster()[sid]["pid_starttime"])
    assert len(capacity.open_grants()) == 1

    rc = cli.main(["kill", sid, "--reason",
                   "The worker is repeating itself on a spec it cannot do."])
    assert rc == 0
    out = capsys.readouterr().out

    # The process, first: a record saying killed is worth nothing while the
    # process it names is still running. It is this test's own child, so
    # it lingers as a zombie until reaped — dead either way, and gone from
    # the table once reaped.
    assert not procs.pid_alive(pid)
    os.waitpid(pid, 0)
    assert procs.proc_starttime(pid) is None
    assert line(out, "stopped: ").startswith(f"pid {pid}")

    record = roster()[sid]
    assert record["state"] == "killed"
    assert record["killed_by"] == "owner"
    assert record["killed_reason"] == \
        "The worker is repeating itself on a spec it cannot do."
    assert record["killed_at"]
    assert record["stopped_pid"] == pid
    assert record["pid"] is None

    # The job line, killed, with the same hand and the same reason.
    job = [entry for entry in jobs("panel") if entry["id"] == job_id][-1]
    assert job["state"] == "killed"
    assert job["killed_by"] == "owner"
    assert job["killed_reason"] == \
        "The worker is repeating itself on a spec it cannot do."
    assert line(out, "job: ") == f"{job_id} killed"

    # And the slot is back, which is the whole point of ending it here
    # rather than by hand.
    assert capacity.open_grants() == []
    assert line(out, "slots released: ") == "1"


def test_a_job_id_kills_the_session_running_it(env, sleeper, capsys):
    """The owner names the work, not the process it happens to run in."""
    add_panel_front()
    capsys.readouterr()
    sid, job_id = start_worker(env, capsys)
    pid = roster()[sid]["pid"]

    assert cli.main(["kill", job_id, "--reason",
                     "This job duplicates one already verified."]) == 0
    out = capsys.readouterr().out
    assert line(out, "killed: ") == sid
    assert not procs.pid_alive(pid)
    assert roster()[sid]["state"] == "killed"
    assert [entry for entry in jobs("panel")
            if entry["id"] == job_id][-1]["state"] == "killed"


def test_kill_stops_the_transient_unit_before_the_pid(
        env, sleeper, capsys, monkeypatch):
    """The unit is the kill group: its cgroup holds children nobody saw.

    A kill that only signalled the recorded pid would leave whatever the
    worker had spawned outside its own tree still running under systemd.
    """
    add_panel_front()
    capsys.readouterr()
    sid, _job = start_worker(env, capsys)

    stopped: list[str | None] = []
    monkeypatch.setattr(_common, "stop_unit",
                        lambda session_id: stopped.append(session_id) or True)
    assert cli.main(["kill", sid, "--reason", "Stopping to re-spec it."]) == 0
    assert stopped == [sid]
    assert line(capsys.readouterr().out, "unit: ") == "stopped"


def test_the_unit_stopped_is_the_recorded_session_and_no_pattern(env):
    """Only this session's own unit name is ever passed to systemctl.

    Fails on a kill that matched processes by name or by pattern, which is
    how a runtime ends somebody else's work.
    """
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Result()

    assert _common.stop_unit("ses-abcdefg", run=fake_run) is True
    assert calls == [["systemctl", "--user", "stop", "foreman-ses-abcdefg"]]


def test_kill_of_a_summoned_supervisor_reads_killed_with_its_reason(
        env, fakes, capsys):
    """A supervisor that stopped working is cleared, and the front is free.

    Until this verb existed the only way out was killing a pid by hand:
    `launch supervisor` refuses while a live one exists, and the roster
    went on calling it running.
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    sid = summon(env, capsys, repo)
    pid = roster()[sid]["pid"]
    assert launch_module.live_front_supervisor("panel")[0] == sid

    assert cli.main(["kill", sid, "--reason",
                     "It has written nothing for half an hour."]) == 0
    out = capsys.readouterr().out
    assert line(out, "role: ") == "supervisor"
    assert line(out, "front: ") == "panel"
    assert line(out, "job: ") == "(none)"

    assert not procs.pid_alive(pid)
    record = roster()[sid]
    assert record["state"] == "killed"
    assert record["killed_reason"] == "It has written nothing for half an hour."
    assert record["killed_by"] == "owner"
    # The front holds no live supervisor now, so one can be summoned.
    assert launch_module.live_front_supervisor("panel") is None


def test_kill_refuses_the_merge_desk_by_name(env, sleeper, capsys,
                                             monkeypatch):
    """The desk holds the merge queue, not the roster: it kills nothing."""
    add_panel_front()
    capsys.readouterr()
    sid, _job = start_worker(env, capsys)
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: roster["sessions"].__setitem__(
            "ses-thedesk",
            Session(id="ses-thedesk", role="merge-desk", pool="opus",
                    model="claude-opus-5", pid=os.getpid(),
                    pid_starttime=procs.proc_starttime(os.getpid()),
                    state="running").to_dict()) or roster,
        default={"sessions": {}})
    monkeypatch.setenv(SESSION_ENV, "ses-thedesk")

    assert cli.main(["kill", sid, "--reason", "Clearing the queue."]) != 0
    assert "role 'merge-desk' may not call 'kill'" in capsys.readouterr().err
    # And the session it aimed at is untouched.
    assert roster()[sid]["state"] == "running"


def test_a_supervisor_kills_only_what_it_launched(env, sleeper, capsys,
                                                  monkeypatch):
    """Authority follows the launch: another front's worker is not yours."""
    add_panel_front()
    capsys.readouterr()
    sid, _job = start_worker(env, capsys)
    for held in ("ses-mysuper", "ses-notmine"):
        store.update_snapshot(
            paths.roster_path(),
            lambda roster, held=held: roster["sessions"].__setitem__(
                held,
                Session(id=held, role="supervisor", pool="opus",
                        model="claude-opus-5", front="panel",
                        pid=os.getpid(),
                        pid_starttime=procs.proc_starttime(os.getpid()),
                        state="running").to_dict()) or roster,
            default={"sessions": {}})
    monkeypatch.setenv(SESSION_ENV, "ses-notmine")

    assert cli.main(["kill", sid, "--reason", "Not my worker."]) != 0
    err = capsys.readouterr().err
    assert f"session '{sid}' was launched by" in err
    assert "a supervisor kills only what it launched" in err
    assert roster()[sid]["state"] == "running"

    # The supervisor that did launch it may end it.
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: roster["sessions"][sid].__setitem__(
            "launched_by", "ses-mysuper") or roster,
        default={"sessions": {}})
    monkeypatch.setenv(SESSION_ENV, "ses-mysuper")
    assert cli.main(["kill", sid, "--reason",
                     "It has re-run the same failing check four times."]) == 0
    assert roster()[sid]["state"] == "killed"
    assert roster()[sid]["killed_by"] == "ses-mysuper"


def test_kill_refuses_without_a_reason_and_on_an_unknown_id(env, capsys):
    """Both refusals name what is wrong, and both name it at once."""
    assert cli.main(["kill", "ses-nothere"]) != 0
    err = capsys.readouterr().err
    assert "field '--reason' is required for 'kill'" in err
    assert "unknown session or job 'ses-nothere'" in err


def test_kill_leaves_a_job_that_already_returned_alone(env, sleeper, capsys):
    """A process stopped afterwards does not undo what it produced."""
    add_panel_front()
    capsys.readouterr()
    sid, job_id = start_worker(env, capsys)
    job = [entry for entry in jobs("panel") if entry["id"] == job_id][-1]
    store.append_ledger(paths.front_jobs_path("panel"),
                        dict(job, state="returned"))

    assert cli.main(["kill", sid, "--reason", "Tidying up after it."]) == 0
    assert "left returned" in capsys.readouterr().out
    assert [entry for entry in jobs("panel")
            if entry["id"] == job_id][-1]["state"] == "returned"


# --------------------------------------------------------------------------
# relaunch
# --------------------------------------------------------------------------


def test_the_relaunched_script_carries_the_prompt_and_never_resumes(
        env, fakes, capsys):
    """The artefact the window runs is where the defect lived.

    The old script ran `claude --resume <id>` with no positional prompt and
    printed a path to the terminal instead. A resumed conversation cannot
    be handed a prompt, so it waited for a person; this asserts on the
    script itself, which is what the window actually executes.
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    sid = summon(env, capsys, repo)
    first_vendor = roster()[sid]["vendor_session"]

    assert cli.main(["relaunch", sid, "--workspace", "6"]) == 0
    out = capsys.readouterr().out
    vendor_id = line(out, "vendor session: ")
    script = (paths.session_dir(sid) / "run.sh").read_text(encoding="utf-8")

    assert "--resume" not in script
    assert "You were relaunched" not in script
    assert f"--session-id {vendor_id}" in script
    # The prompt, positionally: the shell reads the role prompt file into
    # the vendor's last argument.
    assert f'"$(cat {paths.session_dir(sid) / "role-prompt.md"})"' in script
    assert script.index("--session-id") < script.index("$(cat")

    # One Foreman session, a new conversation under it, on the roster.
    assert line(out, "relaunches: ") == sid
    assert vendor_id != first_vendor
    assert roster()[sid]["vendor_session"] == vendor_id
    assert roster()[sid]["replaced_vendor_session"] == first_vendor
    assert roster()[sid]["state"] == "running"
    assert set(roster()) == {sid}


def test_relaunch_carries_the_latest_checkpoint_into_the_prompt(
        env, fakes, capsys, monkeypatch):
    """Continuity is the checkpoint in the prompt, not a resumed history."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    sid = summon(env, capsys, repo)
    monkeypatch.setenv(SESSION_ENV, sid)
    assert cli.main(["checkpoint",
                     "--doing", "Verifying the plugin skeleton job.",
                     "--next", "Plan the blocks task into three jobs."]) == 0
    monkeypatch.delenv(SESSION_ENV)
    capsys.readouterr()

    assert cli.main(["relaunch", sid, "--workspace", "6"]) == 0
    capsys.readouterr()
    prompt = (paths.session_dir(sid) / "role-prompt.md").read_text(
        encoding="utf-8")
    assert "Verifying the plugin skeleton job." in prompt
    assert "Plan the blocks task into three jobs." in prompt
    # Its own checkpoint, named as its own: a session told it replaces
    # itself would read its own words as a stranger's.
    assert "You were relaunched" in prompt
    assert f"Session {sid}, which you replace" not in prompt


def test_launch_supervisor_still_refuses_and_names_the_way_out(
        env, fakes, capsys):
    """One front, one supervisor — and the refusal says how to replace it."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    sid = summon(env, capsys, repo)

    assert cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                     "--repo", str(repo)]) != 0
    err = capsys.readouterr().err
    assert f"already has a live supervisor '{sid}'" in err
    assert f"foreman kill {sid} --reason <why>" in err
    assert f"foreman relaunch {sid}" in err
    assert err.index("foreman kill") < err.index("foreman relaunch")


# --------------------------------------------------------------------------
# the collector's automatic relaunch
# --------------------------------------------------------------------------


def _dead_supervisor(env, repo: Path, sid: str) -> int:
    """One rostered supervisor whose process is really gone. Returns its pid."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    stamp = datetime.now(timezone.utc).isoformat()
    paths.session_dir(sid).mkdir(parents=True, exist_ok=True)
    store.write_snapshot(paths.roster_path(), {"sessions": {
        sid: Session(
            id=sid, role="supervisor", pool="opus", model="claude-opus-5",
            front="panel", pid=proc.pid,
            # A start time no live process can match, so the record is dead
            # whether or not the number has since been reused.
            pid_starttime=1,
            vendor_session="the-dead-conversation",
            worktree=str(repo), started_at=stamp, last_declared_at=stamp,
            state="running").to_dict()}})
    return proc.pid


def test_the_collector_summons_a_dead_supervisor_anew(env, fakes, capsys):
    """Design flow 4, unattended: it dies, it comes back, it continues.

    Fails on the collector this replaces, which opened an anomaly saying a
    person must relaunch and left the front with no supervisor until one
    read it.
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    sid = "ses-deadone"
    dead_pid = _dead_supervisor(env, repo, sid)
    store.write_snapshot(paths.checkpoint_path(sid),
                         {"doing": "Verifying the plugin skeleton job.",
                          "next": "Plan the blocks task into three jobs."})

    collector.tick()

    # Same Foreman session id, a live process, a new vendor conversation.
    record = roster()[sid]
    assert record["state"] == "running"
    assert record["pid"] != dead_pid
    assert procs.same_process(record["pid"], record["pid_starttime"])
    assert record["vendor_session"] not in (None, "the-dead-conversation")
    assert record["relaunched_by"] == "collector"
    assert record["relaunches"] == 1
    assert set(roster()) == {sid}

    # Through the same path the verb takes: prompt positionally, no resume,
    # and the checkpoint it left inside that prompt.
    received = fakes["claude_argv"].read_text(encoding="utf-8")
    assert "--resume" not in received
    assert f"--session-id\n{record['vendor_session']}" in received
    assert "Verifying the plugin skeleton job." in received

    # And nothing is left on the screen telling a person to do it by hand.
    assert not _open_anomalies("supervisor dead", sid)


def test_the_collector_stops_relaunching_after_the_limit(env, fakes, capsys):
    """A supervisor that dies on every summon is not summoned forever."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    sid = "ses-deadtwo"
    _dead_supervisor(env, repo, sid)
    state = store.read_snapshot(paths.collector_path(), default={})
    now = store.utcnow_iso()
    store.write_snapshot(paths.collector_path(), dict(
        state, sessions={}, parents={},
        relaunches=[{"from": sid, "at": now}, {"from": sid, "at": now}]))

    collector.tick()

    assert roster()[sid]["state"] == "exited"
    open_lines = _open_anomalies("supervisor dead", sid)
    assert open_lines
    assert "automatic relaunch(es)" in open_lines[-1]["detail"]
    assert "a person must relaunch it" in open_lines[-1]["detail"]


def _open_anomalies(kind: str, subject: str) -> list[dict]:
    open_lines: dict[tuple, dict] = {}
    for record in store.read_ledger(paths.anomalies_path()):
        if record.get("kind") != kind or record.get("subject") != subject:
            continue
        key = (record.get("kind"), record.get("subject"))
        if record.get("resolved_at") is None:
            open_lines[key] = record
        else:
            open_lines.pop(key, None)
    return list(open_lines.values())


def test_relaunch_refuses_a_dirty_checkout_and_names_the_files(
        env, fakes, capsys):
    """Uncommitted work at relaunch is the supervisor's call, not the
    launcher's.

    A session summoned into a checkout carrying changes nobody accounted
    for either commits somebody else's work as its own or throws it away.
    Seen on the panel front: a dead job's worktree left two modified files
    on top of its last commit. The refusal names them, so the supervisor
    can rule keep or discard and ask again.
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    sid = summon(env, capsys, repo)

    (repo / "half-done.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "notes.md").write_text("what I was doing\n", encoding="utf-8")

    assert cli.main(["relaunch", sid, "--workspace", "6"]) == 1
    err = capsys.readouterr().err
    assert "uncommitted changes" in err
    assert "half-done.py" in err and "notes.md" in err
    assert "keep it" in err and "discard it" in err
    # Refused before anything moved: the roster still carries the session
    # it had, on the conversation it had.
    assert roster()[sid]["state"] == "running"


def test_relaunch_takes_a_clean_checkout_after_the_work_is_kept(
        env, fakes, capsys):
    """The way out the refusal names actually works."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    sid = summon(env, capsys, repo)

    (repo / "half-done.py").write_text("x = 1\n", encoding="utf-8")
    assert cli.main(["relaunch", sid, "--workspace", "6"]) == 1
    capsys.readouterr()

    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "-m", "kept"],
                   check=True)
    assert cli.main(["relaunch", sid, "--workspace", "6"]) == 0


def test_a_dirty_check_that_cannot_read_the_tree_reports_nothing(tmp_path):
    """An unreadable checkout is not evidence of a dirty one.

    The branch check refuses a directory that is not a git repository
    before this runs, so silence here must not become a refusal of its
    own.
    """
    assert launch_module.uncommitted_paths(str(tmp_path / "nowhere")) == []
    assert launch_module.dirty_tree_problem(str(tmp_path / "nowhere")) is None
