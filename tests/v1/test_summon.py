"""Summoning a supervisor: the role prompt, the roster, register, relaunch.

Every test drives the real entry points against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``. Nothing here starts a real
Claude session, opens a real window, or writes to a real state directory:
a fake ``claude`` and a fake window launcher sit in a tmp ``bin`` and
record their argv, exactly as the v0 launcher tests put a fake vendor
command on PATH. The break each test catches is a supervisor that starts
without an identity, a prompt with a section the supervisor needed and did
not get, a roster entry the collector cannot tell from a reused pid, or a
resume that sits idle because it was handed a positional prompt.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, collector, paths, procs, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.entities import Session

ROOT = Path(__file__).resolve().parents[2]
PANEL_BRIEF = ROOT / "briefs" / "panel"

RULING = "Verify by re-running; a worker's word is PLAUSIBLE until then."


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A whole machine's worth of Foreman state, inside tmp_path."""
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(launch_module.VENDOR_CONFIG_ENV,
                       str(tmp_path / "home" / "vendor.json"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    # The proven shape leaves the window open for two minutes so it does
    # not close on its own last words; a test that spawned that would leak
    # a sleeping process per launch.
    monkeypatch.setattr(launch_module, "WINDOW_LINGER_SECONDS", 0)
    return tmp_path


@pytest.fixture()
def fakes(env, monkeypatch):
    """A fake ``claude`` and a fake window launcher, both recording argv.

    The launcher execs what it is handed, so the wrapper really runs: it
    writes its own pid file, which is the pid the launcher reads back.
    """
    bindir = env / "bin"
    bindir.mkdir()
    record = env / "launcher-argv"
    vendor = env / "claude-argv"

    launcher = bindir / "fake-window"
    launcher.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {record}\n'
        f'printf "AGENT_WS=%s\\n" "${{AGENT_WS-}}" >> {record}\n'
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
    return {"launcher_argv": record, "claude_argv": vendor}


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


def add_panel_front() -> None:
    assert cli.main(["front", "add", str(PANEL_BRIEF)]) == 0


def line(out: str, prefix: str) -> str:
    for row in out.splitlines():
        if row.startswith(prefix):
            return row.split(prefix, 1)[1].strip()
    raise AssertionError(f"no {prefix!r} line in output:\n{out}")


def roster() -> dict:
    return store.read_snapshot(paths.roster_path(),
                               default={"sessions": {}})["sessions"]


def brief_field(name: str) -> str:
    """A field read out of the brief itself, so 'verbatim' means verbatim."""
    text = (PANEL_BRIEF / "brief.toml").read_text(encoding="utf-8")
    for row in text.splitlines():
        if row.startswith(name):
            return row.split("=", 1)[1].strip().strip('"')
    raise AssertionError(f"no {name} in the brief")


# --------------------------------------------------------------------------
# The role prompt
# --------------------------------------------------------------------------


def test_dry_run_prompt_carries_every_section_filled(env, capsys):
    """Every section a supervisor needs, with content, not just headings."""
    make_repo(env / "repo")
    add_panel_front()
    store.append_ledger(paths.rulings_path(),
                        {"scope": "swarm", "text": RULING, "source": "owner"})
    capsys.readouterr()

    rc = cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                   "--repo", str(env / "repo"), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    sid = line(out, "session: ")
    prompt = Path(line(out, "role prompt: ")).read_text(encoding="utf-8")

    # Nothing left for the supervisor to guess: no placeholder survives.
    assert "{{" not in prompt and "}}" not in prompt
    # Which front it owns, and its own identity.
    assert "supervisor of front panel" in prompt
    assert sid in prompt
    # The front's want and done-when, verbatim from the brief.
    assert brief_field("done-when") in prompt
    assert "Quickshell plugin, toggled by Super+M" in prompt
    # Every task, with its state, its size and what it comes after.
    assert ('- "plugin skeleton and data feed" — ready · size 1 · '
            'after nothing') in prompt
    assert ('- "blocks in the v0 layout" — waiting · size 7 · '
            'after plugin skeleton and data feed') in prompt
    assert '- "keys and actions" — waiting · size 1 · ' in prompt
    assert '- "bar widget and install" — waiting · size 1 · ' in prompt
    # What it reports and when: the four kinds and nothing else.
    assert "foreman checkpoint" in prompt
    assert "every state change" in prompt
    assert "twenty minutes" in prompt
    assert "money, an irreversible act" in prompt
    # The allocation, per role, as a ceiling. The panel brief allocates
    # three muse, one opus, one astra and no grok.
    assert "- muse: at most 3 at once" in prompt
    assert "- opus: at most 1 at once" in prompt
    assert "- astra: at most 1 at once" in prompt
    assert "- grok: none" in prompt
    assert "ceiling" in prompt
    # The verbs it may call, each one of them, and no invented substitute.
    for verb, _why in launch_module.SUPERVISOR_VERBS:
        assert verb in prompt
    for unshipped in launch_module.SUPERVISOR_VERBS_UNSHIPPED:
        assert unshipped in prompt
    # The rulings, verbatim.
    assert RULING in prompt
    # The environment contract, every part of it.
    assert f"- repository: {env / 'repo'}" in prompt
    assert "- branch: main" in prompt
    assert f"- state directory: {paths.state_dir()}" in prompt
    assert f"- your session id: {sid}" in prompt
    assert f"{SESSION_ENV}: must be set to {sid} on every" in prompt
    # A fresh launch has no predecessor, and says so rather than leaving
    # the heading empty.
    assert "you are the first supervisor summoned for this front" in prompt

    # The dry run prints the prompt itself: it is the thing being reviewed.
    assert prompt.strip() in out


def test_dry_run_records_nothing_and_starts_nothing(env, capsys):
    make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(env / "repo"), "--dry-run"]) == 0
    out = capsys.readouterr().out
    sid = line(out, "session: ")
    assert "(not started --dry-run)" in out
    assert roster() == {}
    assert not paths.session_pid_path(sid).exists()


# --------------------------------------------------------------------------
# The launch
# --------------------------------------------------------------------------


def test_launch_puts_the_supervisor_on_the_roster_with_a_true_pid(
        env, fakes, capsys):
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()

    rc = cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                   "--repo", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    sid = line(out, "session: ")
    vendor_id = line(out, "vendor session: ")
    pid = int(line(out, "pid: "))

    record = roster()[sid]
    assert record["role"] == "supervisor"
    assert record["pool"] == "opus"
    assert record["model"] == "claude-opus-5"
    assert record["front"] == "panel"
    assert record["state"] == "running"
    # The pid is the window's own, written by the wrapper as its first act
    # and read back — never the launcher's.
    assert pid != os.getpid()
    assert record["pid"] == pid
    assert paths.session_pid_path(sid).read_text(
        encoding="utf-8").strip() == str(pid)
    # The start time beside the pid is what tells this process apart from a
    # later one that reuses the number.
    assert isinstance(record["pid_starttime"], int)

    # Foreman mints the roster id; the vendor needs a uuid of its own, kept
    # beside the session so a relaunch can resume the same conversation.
    stored = (paths.session_dir(sid) / "vendor-session").read_text(
        encoding="utf-8").strip()
    assert stored == vendor_id
    assert len(vendor_id) == 36 and vendor_id.count("-") == 4

    # The printed command is the proven shape.
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=40" in out
    assert "--model claude-opus-5" in out
    assert f"--session-id {vendor_id}" in out
    assert "--dangerously-skip-permissions" in out
    assert f'"$(cat {paths.session_dir(sid) / "role-prompt.md"})"' in out


def test_launch_goes_through_the_window_helper_on_the_asked_workspace(
        env, fakes, capsys):
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                     "--repo", str(repo)]) == 0
    sid = line(capsys.readouterr().out, "session: ")

    argv = fakes["launcher_argv"].read_text(encoding="utf-8").splitlines()
    # The helper is handed a window name and the command to run, and reads
    # the workspace from AGENT_WS. Never reimplemented here.
    assert argv[0] == f"foreman-{sid}"
    assert argv[1] == "bash"
    assert argv[2] == str(paths.session_dir(sid) / "run.sh")
    assert "AGENT_WS=6" in argv

    # The vendor really ran, with the role prompt as its positional prompt.
    vendor = fakes["claude_argv"].read_text(encoding="utf-8")
    assert "--session-id" in vendor
    assert "--dangerously-skip-permissions" in vendor
    assert "supervisor of front panel" in vendor


def test_launch_pre_answers_both_first_launch_dialogs(env, fakes, capsys):
    """A window sitting on a question nobody will click is a dead launch."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(repo)]) == 0
    capsys.readouterr()
    config = json.loads(
        launch_module.vendor_config_path().read_text(encoding="utf-8"))
    assert config["bypassPermissionsModeAccepted"] is True
    assert config["projects"][str(repo)]["hasTrustDialogAccepted"] is True


def test_unreadable_vendor_config_is_left_alone(env, fakes, capsys):
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    config = launch_module.vendor_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{not json at all", encoding="utf-8")
    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(repo)]) == 0
    capsys.readouterr()
    assert config.read_text(encoding="utf-8") == "{not json at all"


def test_launch_for_a_front_with_no_record_is_refused_by_name(env, capsys):
    make_repo(env / "repo")
    assert cli.main(["launch", "supervisor", "nosuchfront", "--repo",
                     str(env / "repo"), "--dry-run"]) != 0
    err = capsys.readouterr().err
    assert "unknown front 'nosuchfront'" in err
    assert "no record on the ledger" in err
    assert roster() == {}


def test_launch_while_frozen_is_refused(env, capsys):
    make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    paths.ensure_state_tree()
    paths.frozen_path().write_text("owner froze it\n", encoding="utf-8")
    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(env / "repo"), "--dry-run"]) != 0
    assert "refuses while frozen" in capsys.readouterr().err


def test_bad_workspace_is_refused_by_name(env, capsys):
    make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--workspace", "six",
                     "--repo", str(env / "repo"), "--dry-run"]) != 0
    assert "bad workspace" in capsys.readouterr().err


def test_the_window_helper_defaults_to_the_shipped_skill(env, monkeypatch):
    """Configurable, with the skills location as its default."""
    from foreman.pools import _common

    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    default = _common.window_launcher_or_default()
    assert default.endswith("/skills/muse-workers/launch-window.sh")
    monkeypatch.setenv("FOREMAN_WINDOW_LAUNCHER", "chosen-launcher")
    assert _common.window_launcher_or_default() == "chosen-launcher"


def test_the_proven_shape_leaves_the_window_open():
    """The window closes itself, and not on the session's own last words.

    Takes no fixture on purpose: the ``env`` fixture shortens this so a
    test does not leak a sleeping process per launch, and the shipped
    value is what a real summon runs.
    """
    assert launch_module.WINDOW_LINGER_SECONDS == 120


# --------------------------------------------------------------------------
# register
# --------------------------------------------------------------------------


def test_register_puts_a_session_on_the_roster_with_its_start_time(
        env, capsys):
    rc = cli.main(["register", "--role", "supervisor", "--front", "panel",
                   "--pid", str(os.getpid()), "--session", "a-vendor-uuid"])
    assert rc == 0
    sid = line(capsys.readouterr().out, "session: ")
    record = roster()[sid]
    assert record["role"] == "supervisor"
    assert record["front"] == "panel"
    assert record["pid"] == os.getpid()
    assert record["state"] == "running"
    # Beside the pid, the identity: without it the collector cannot tell
    # this process from a later one that reuses its number.
    assert record["pid_starttime"] == procs.proc_starttime(os.getpid())
    assert procs.same_process(record["pid"], record["pid_starttime"])
    assert (paths.session_dir(sid) / "vendor-session").read_text(
        encoding="utf-8").strip() == "a-vendor-uuid"


def test_register_refuses_a_pid_that_is_not_running(env, capsys):
    dead = subprocess.Popen(["true"])
    dead.wait()
    # A pid the roster would believe forever, for a process that is gone.
    assert cli.main(["register", "--role", "supervisor", "--front", "panel",
                     "--pid", str(dead.pid)]) != 0
    assert "is not running" in capsys.readouterr().err
    assert roster() == {}


def test_register_refuses_a_role_the_roster_does_not_know(env, capsys):
    assert cli.main(["register", "--role", "gardener", "--front", "panel",
                     "--pid", str(os.getpid())]) != 0
    err = capsys.readouterr().err
    assert "unknown role 'gardener'" in err
    assert "supervisor" in err
    assert roster() == {}


def test_register_covers_the_orchestrator_which_owns_no_front(env, capsys):
    assert cli.main(["register", "--role", "foreman",
                     "--pid", str(os.getpid())]) == 0
    sid = line(capsys.readouterr().out, "session: ")
    record = roster()[sid]
    assert record["front"] is None
    assert record["model"] == "claude-opus-5"


# --------------------------------------------------------------------------
# relaunch
# --------------------------------------------------------------------------


def _summon(env, repo: Path, capsys) -> str:
    assert cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                     "--repo", str(repo)]) == 0
    return line(capsys.readouterr().out, "session: ")


def test_relaunch_replays_the_checkpoint_and_resumes_the_conversation(
        env, fakes, capsys, monkeypatch):
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    old = _summon(env, repo, capsys)
    vendor_id = (paths.session_dir(old) / "vendor-session").read_text(
        encoding="utf-8").strip()

    monkeypatch.setenv(SESSION_ENV, old)
    assert cli.main(["checkpoint",
                     "--doing", "Verifying the plugin skeleton job.",
                     "--next", "Plan the blocks task into three jobs.",
                     "--held", "muse worker on plugin skeleton"]) == 0
    monkeypatch.delenv(SESSION_ENV)
    capsys.readouterr()

    assert cli.main(["relaunch", old, "--workspace", "6"]) == 0
    out = capsys.readouterr().out
    new = line(out, "session: ")
    assert new != old
    assert line(out, "replaces: ") == old

    prompt = Path(line(out, "role prompt: ")).read_text(encoding="utf-8")
    # The predecessor's own words, under their own heading, so the
    # successor picks up where it was left rather than starting over.
    assert "## Your predecessor's last checkpoint" in prompt
    assert f"Session {old}, which you replace" in prompt
    assert "Verifying the plugin skeleton job." in prompt
    assert "Plan the blocks task into three jobs." in prompt
    assert "muse worker on plugin skeleton" in prompt
    # And it is a fresh prompt, not the old one: its own identity, and the
    # front's tasks as they stand now.
    assert new in prompt
    assert '- "plugin skeleton and data feed" — ready' in prompt

    # Resume, with the vendor id, and no positional prompt: a --resume with
    # one sits idle and never starts.
    vendor = fakes["claude_argv"].read_text(
        encoding="utf-8").strip().splitlines()
    assert vendor == ["--resume", vendor_id, "--model", "claude-opus-5",
                      "--dangerously-skip-permissions"]
    assert "--session-id" not in out

    # The predecessor leaves the roster; the successor holds the front.
    assert roster()[old]["state"] == "exited"
    assert roster()[new]["state"] == "running"
    assert roster()[new]["front"] == "panel"
    assert roster()[new]["launched_by"] == old
    # The successor answers to the same conversation, so it can be
    # relaunched in its turn.
    assert (paths.session_dir(new) / "vendor-session").read_text(
        encoding="utf-8").strip() == vendor_id


def test_relaunch_refuses_an_unknown_session_by_name(env, capsys):
    assert cli.main(["relaunch", "ses-nothere"]) != 0
    assert "unknown session 'ses-nothere'" in capsys.readouterr().err


def test_relaunch_refuses_a_session_with_no_vendor_id(env, capsys):
    add_panel_front()
    capsys.readouterr()
    store.write_snapshot(paths.roster_path(), {"sessions": {
        "ses-handmade": Session(id="ses-handmade", role="supervisor",
                                pool="opus", model="claude-opus-5",
                                front="panel", state="running").to_dict()}})
    assert cli.main(["relaunch", "ses-handmade", "--dry-run"]) != 0
    err = capsys.readouterr().err
    assert "no vendor session id" in err
    assert "no conversation to resume" in err


# --------------------------------------------------------------------------
# The collector's watch on a summoned supervisor
# --------------------------------------------------------------------------


def _roster_supervisor(env, sid: str, declared_minutes_ago: float) -> None:
    """One rostered, live supervisor whose last declared write is old."""
    stamp = (datetime.now(timezone.utc)
             - timedelta(minutes=declared_minutes_ago)).isoformat()
    store.write_snapshot(paths.roster_path(), {"sessions": {
        sid: Session(
            id=sid, role="supervisor", pool="opus", model="claude-opus-5",
            front="panel", pid=os.getpid(),
            pid_starttime=procs.proc_starttime(os.getpid()),
            worktree=str(env / "not-a-real-worktree"),
            started_at=stamp, last_declared_at=stamp,
            state="running").to_dict()}})


def _open_anomalies(kind: str, subject: str) -> list[dict]:
    open_lines: dict[tuple, dict] = {}
    for record in store.read_ledger(paths.anomalies_path()):
        key = (record.get("kind"), record.get("subject"))
        open_lines[key] = record
    hit = open_lines.get((kind, subject))
    return [hit] if hit is not None and hit.get("resolved_at") is None else []


def test_collector_flags_a_silent_supervisor_and_a_checkpoint_clears_it(
        env, capsys, monkeypatch):
    add_panel_front()
    capsys.readouterr()
    sid = "ses-silent1"
    _roster_supervisor(env, sid, declared_minutes_ago=40)
    config = collector.CollectorConfig(supervisor_silent_seconds=15 * 60)

    collector.tick(config=config)
    flagged = _open_anomalies("supervisor silent", sid)
    assert flagged, "a rostered supervisor past the threshold was not flagged"
    assert sid in flagged[0]["detail"]
    assert "no jobs" in flagged[0]["detail"]

    # A checkpoint is the declared write the anomaly is measured against.
    monkeypatch.setenv(SESSION_ENV, sid)
    assert cli.main(["checkpoint", "--doing", "Reading the front ledger.",
                     "--next", "Plan the first task."]) == 0
    monkeypatch.delenv(SESSION_ENV)
    capsys.readouterr()

    collector.tick(config=config)
    assert _open_anomalies("supervisor silent", sid) == []


def test_a_supervisor_running_a_job_is_not_silent(env, capsys):
    """Silence is only silence when nothing is running under it."""
    add_panel_front()
    capsys.readouterr()
    sid = "ses-silent2"
    _roster_supervisor(env, sid, declared_minutes_ago=40)
    worker = subprocess.Popen(["sleep", "30"])
    try:
        roster_now = store.read_snapshot(paths.roster_path())
        roster_now["sessions"]["ses-worker1"] = Session(
            id="ses-worker1", role="muse", pool="claude", model="claude",
            front="panel", job="job-1", pid=worker.pid,
            pid_starttime=procs.proc_starttime(worker.pid),
            started_at=datetime.now(timezone.utc).isoformat(),
            state="running").to_dict()
        store.write_snapshot(paths.roster_path(), roster_now)
        collector.tick(config=collector.CollectorConfig(
            supervisor_silent_seconds=15 * 60))
        assert _open_anomalies("supervisor silent", sid) == []
    finally:
        worker.kill()
        worker.wait()


# --------------------------------------------------------------------------
# The worker shape is untouched
# --------------------------------------------------------------------------


def test_a_worker_launch_still_needs_its_spec(env, capsys):
    make_repo(env / "repo")
    assert cli.main(["launch", "muse", "claude", "--repo",
                     str(env / "repo")]) != 0
    assert "spec is required" in capsys.readouterr().err
