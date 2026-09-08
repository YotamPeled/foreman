"""Summoning a supervisor: the role prompt, the roster, register, relaunch.

Every test drives the real entry points against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``. Nothing here starts a real
Claude session, opens a real window, or writes to a real state directory:
a fake ``claude`` and a fake window launcher sit in a tmp ``bin`` and
record their argv, exactly as the v0 launcher tests put a fake vendor
command on PATH. The fake launcher also copies the roster, the role prompt
and the vendor session id at its own entry, so what the launcher recorded
*before* the process started is a fact a test can assert, not an artefact
of reading the final snapshot.

The break each test catches is a supervisor that starts without an
identity, a prompt with a section the supervisor needed and did not get, a
roster entry the collector cannot tell from a reused pid, a resume that
sits idle because it was handed a positional prompt, a fresh prompt that
reaches no session, or a second live supervisor on a front that already
has one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, collector, paths, procs, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV, SUPERVISOR
from foreman import caller as caller_module
from foreman.entities import Session

ROOT = Path(__file__).resolve().parents[2]
PANEL_BRIEF = ROOT / "briefs" / "panel"

RULING = "Verify by re-running; a worker's word is PLAUSIBLE until then."

#: The commands a supervisor must be able to call in this checkout, written
#: out here rather than looped out of the production constants: a list that
#: reads itself proves nothing, and the drift being caught is exactly the
#: prompt disagreeing with the CLI.
SUPERVISOR_COMMANDS = (
    "foreman checkpoint --doing <doing> --next <next>",
    "foreman ask <question ...> --kind <kind> --recommend <recommend>",
    "foreman launch <role> [<pool>] [<spec>]",
    "foreman relaunch <session>",
    "foreman register --role <role> --pid <pid>",
    "foreman job verify <job> --confirmed --command <command> --output <output>",
    "foreman job fail <job> --finding <finding>",
    "foreman task built <task>",
    "foreman task landed <task> --head <head>",
    "foreman evidence --on <on> --claim <claim> --status <status> --command <command>",
    "foreman finding --on <on> --class <class> --title <title> --detail <detail>",
    "foreman measure <front> <monitor> --value <value> --of <of> "
    "--command <command> --output <output>",
    "foreman rule ack <id>",
    "foreman rule list",
    "foreman status",
    "foreman merge request [<branch>] --front <front> --tasks <tasks ...> "
    "--target <target>",
)
#: Verbs this checkout ships for somebody else. A supervisor told it may
#: call one of these is told to walk into a refusal.
NOT_THE_SUPERVISORS = (
    "foreman front add", "foreman front list", "foreman front prefer",
    "foreman front close", "foreman answer", "foreman inbox", "foreman cap",
    "foreman collector",
)
#: The design's supervisor row that this checkout does not ship. Named as
#: missing, never as callable.
UNSHIPPED = ("task ready", "job plan", "job order", "front done")


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
    # not close on its own last words. A test must not wait that long, but
    # it must not shorten it to nothing either: the launcher now refuses a
    # window that is already gone, so a fake window has to live long enough
    # to be confirmed. Five seconds, and every one of them killed below.
    monkeypatch.setattr(launch_module, "WINDOW_LINGER_SECONDS", 5)
    yield tmp_path
    _kill_windows(tmp_path)


def _kill_windows(root: Path) -> None:
    """No window this test opened outlives it."""
    sessions = store.read_snapshot(root / "state" / "roster.json",
                                   default={"sessions": {}})
    for record in (sessions.get("sessions") or {}).values():
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        # A window is a session leader in its own group, which is what
        # tells it apart from the pids these tests register by hand — this
        # process among them. Identity next: a pid alone could name
        # somebody else's process by the time this runs.
        if pid != record.get("pgid") or pid == os.getpid():
            continue
        if not procs.same_process(pid, record.get("pid_starttime")):
            continue
        procs.kill_job(pid, record.get("pgid"))
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass


@pytest.fixture()
def fakes(env, monkeypatch):
    """A fake ``claude`` and a fake window launcher, both recording argv.

    The launcher execs what it is handed, so the wrapper really runs: it
    writes its own pid file, which is the pid the launcher reads back.
    Before it execs anything it copies the roster, the role prompt and the
    vendor session id, so a test can ask what was recorded at the moment
    the process started rather than only what the launch settled on.
    """
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
    """Every section a supervisor needs, with content, not just headings.

    A supervisor whose only instruction is this file must be able to plan a
    job from it: the work, the verification, the rules it runs under and
    the absolute path of everything it is told to read.
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    store.append_ledger(paths.rulings_path(),
                        {"scope": "swarm", "text": RULING, "source": "owner"})
    capsys.readouterr()

    rc = cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                   "--repo", str(repo), "--dry-run"])
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
            'units 0/1 · after nothing') in prompt
    assert ('- "blocks in the v0 layout" — waiting · size 7 · units 0/7 · '
            'after plugin skeleton and data feed') in prompt
    assert '- "keys and actions" — waiting · size 1 · ' in prompt
    assert '- "bar widget and install" — waiting · size 1 · ' in prompt
    # And the work itself: every scope verbatim, every verification command.
    # Titles and states alone are a list of names, not a job to plan.
    assert "WHAT: an Omarchy shell plugin directory" in prompt
    assert "OUT OF SCOPE: any graphic beyond the mock's bars." in prompt
    assert "INPUTS: the mock's key map; §12 verbs." in prompt
    assert "OUTPUTS: `install` and `uninstall`; verified on this machine." \
        in prompt
    for command in ("foreman-verify panel --loads",
                    "foreman-verify panel --blocks",
                    "foreman-verify panel --keys",
                    "foreman-verify panel --install-roundtrip"):
        assert f"`{command}`" in prompt
    # The monitor, its command and its cadence.
    assert "how many of the §13 blocks render from the fixture?" in prompt
    assert "`foreman-verify panel --blocks-count`" in prompt
    assert "in blocks of 8, every landing" in prompt
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
    # Every verb this checkout ships for the role, with its arguments, and
    # no verb that belongs to somebody else.
    for command in SUPERVISOR_COMMANDS:
        assert f"`{command}`" in prompt, command
    for command in NOT_THE_SUPERVISORS:
        assert f"`{command}" not in prompt, command
    for verb in UNSHIPPED:
        assert f"`{verb}`" in prompt, verb
    assert "does not ship them" in prompt
    # The rulings, verbatim: the ledger's and the brief's own, which
    # `front add` records nowhere else.
    assert f"- from the rulings ledger: {RULING}" in prompt
    assert ("- from the brief: Never edit /usr/share/omarchy; clone, never "
            "patch; the state directory is the only input.") in prompt
    # The environment contract, every part of it, every path absolute.
    assert f"- repository: {repo}" in prompt
    assert "- branch: main" in prompt
    assert f"- state directory: {paths.state_dir()}" in prompt
    assert f"- your session id: {sid}" in prompt
    assert f"{SESSION_ENV}: must be set to {sid} on every" in prompt
    assert f"- your checkpoint: {paths.checkpoint_path(sid)}" in prompt
    assert f"- your role prompt: {paths.session_dir(sid) / 'role-prompt.md'}" \
        in prompt
    assert str(launch_module.front_prompt_path("panel")) in prompt
    assert f"the only input this front has: {paths.brief_path('panel')}" \
        in prompt
    assert f"- the front ledger: {paths.front_record_path('panel')}" in prompt
    assert f"- the task ledger: {paths.front_tasks_path('panel')}" in prompt
    assert f"- the job ledger: {paths.front_jobs_path('panel')}" in prompt
    assert f"- the rulings ledger: {paths.rulings_path()}" in prompt
    assert f"- the configuration: {paths.config_file()}" in prompt
    # The brief quotes repository-relative paths; the prompt says what they
    # are relative to, in absolute form.
    assert str(repo / "docs/DESIGN.md") in prompt
    # A fresh launch has no predecessor, and says so rather than leaving
    # the heading empty.
    assert "you are the first supervisor summoned for this front" in prompt

    # The dry run prints the prompt itself: it is the thing being reviewed.
    assert prompt.strip() in out


def test_a_verb_added_to_the_cli_appears_in_the_prompt(env, capsys,
                                                       monkeypatch):
    """The verb list is read out of the CLI, never kept beside it.

    The break this catches is the one the reviewer found: a hand-written
    list that went on forbidding verbs the checkout had started shipping.
    """
    make_repo(env / "repo")
    add_panel_front()
    monkeypatch.setitem(cli.SUBCOMMANDS, "trial",
                        (_trial_verb, {"help": "A verb added for one test."}))
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(env / "repo"), "--dry-run"]) == 0
    prompt = Path(line(capsys.readouterr().out, "role prompt: ")).read_text(
        encoding="utf-8")
    assert "`foreman trial` — A verb added for one test." in prompt


def _trial_verb(args) -> int:
    """A verb registered inside one test only, gated to the supervisor.

    Written the way every other verb is written, because how the prompt
    finds it is by reading that gate.
    """
    me, violations = caller_module.resolve("trial")
    caller_module.check_role(me, "trial", SUPERVISOR, violations=violations)
    return 0


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
    # Not even the front's published prompt: a dry run leaves the state
    # directory as it found it.
    assert not launch_module.front_prompt_path("panel").exists()


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
    # later one that reuses the number, and it is a real one: the launch
    # confirmed the process before recording it.
    assert isinstance(record["pid_starttime"], int)
    assert procs.same_process(record["pid"], record["pid_starttime"])

    # The identity was on the roster before the process existed, not after
    # it answered: the fake launcher read the roster at its own entry.
    at_entry = json.loads(
        (fakes["seen"] / "roster.json").read_text(encoding="utf-8"))
    assert at_entry["sessions"][sid]["state"] == "starting"
    assert at_entry["sessions"][sid]["front"] == "panel"
    # And so were the prompt and the vendor id it answers to.
    assert "supervisor of front panel" in (
        fakes["seen"] / "role-prompt.md").read_text(encoding="utf-8")
    assert (fakes["seen"] / "vendor-session").read_text(
        encoding="utf-8").strip() == vendor_id

    # Foreman mints the roster id; the vendor needs a uuid of its own, kept
    # beside the session so a relaunch can resume the same conversation.
    stored = (paths.session_dir(sid) / "vendor-session").read_text(
        encoding="utf-8").strip()
    assert stored == vendor_id
    assert len(vendor_id) == 36 and vendor_id.count("-") == 4

    # The front's published prompt is this session's, so a relaunched
    # successor reading that path reads the newest one.
    assert launch_module.front_prompt_path("panel").read_text(
        encoding="utf-8") == (
            paths.session_dir(sid) / "role-prompt.md").read_text(
                encoding="utf-8")

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


def test_a_second_supervisor_for_a_live_front_is_refused_by_name(
        env, fakes, capsys):
    """One front, one supervisor.

    Two of them, both authorised to plan and dispatch on the same front, is
    the failure this runtime exists to prevent.
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(repo)]) == 0
    first = line(capsys.readouterr().out, "session: ")

    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(repo)]) != 0
    err = capsys.readouterr().err
    assert "already has a live supervisor" in err
    assert first in err
    assert "foreman relaunch" in err
    # Refused before anything was minted: the roster still holds one.
    running = [sid for sid, record in roster().items()
               if record.get("state") == "running"]
    assert running == [first]


def test_a_front_whose_supervisor_is_gone_can_be_summoned_again(
        env, fakes, capsys):
    """A stale record holds no front: liveness is the process, not the row."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(repo)]) == 0
    first = line(capsys.readouterr().out, "session: ")
    dead = roster()[first]
    procs.kill_job(dead["pid"], dead.get("pgid"))
    try:
        os.waitpid(dead["pid"], 0)
    except (ChildProcessError, OSError):
        pass

    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(repo)]) == 0
    second = line(capsys.readouterr().out, "session: ")
    assert second != first
    assert roster()[second]["state"] == "running"


def test_a_window_that_is_already_gone_is_never_recorded_running(
        env, capsys, monkeypatch):
    """A pid file is not proof that a process lives.

    The wrapper writes its pid as its first act; if it dies before the
    launcher reads the file, the old code reported success and rostered a
    session as running with no process start time — and a null identity is
    what the collector reads as "trust it".
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    bindir = env / "bin"
    bindir.mkdir()
    # A launcher whose window writes a pid file for a process that has
    # already exited, and runs no vendor at all.
    launcher = bindir / "gone-window"
    launcher.write_text(
        "#!/bin/sh\n"
        'sid=${1#foreman-}\n'
        f'sh -c "echo \\$\\$ > {env}/state/sessions/$sid/pid"\n',
        encoding="utf-8",
    )
    os.chmod(launcher, 0o755)
    monkeypatch.setenv("FOREMAN_WINDOW_LAUNCHER", str(launcher))
    capsys.readouterr()

    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(repo)]) != 0
    err = capsys.readouterr().err
    assert "failed to start the supervisor" in err
    assert "before the launcher could confirm it" in err
    states = [record.get("state") for record in roster().values()]
    assert states == ["failed"]
    # Never a running record without an identity beside the pid.
    assert not [record for record in roster().values()
                if record.get("state") == "running"]


def test_a_failure_after_the_roster_write_closes_the_session(
        env, fakes, capsys, monkeypatch):
    """Every step from the roster write to the spawn is inside the handler.

    The break this catches is an exception escaping the launcher with a
    `starting` session left on the roster forever.
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()

    def refuse_to_write(path, inner):
        raise PermissionError("injected script write failure")

    monkeypatch.setattr(launch_module._common, "write_worker_script",
                        refuse_to_write)
    assert cli.main(["launch", "supervisor", "panel", "--repo",
                     str(repo)]) != 0
    err = capsys.readouterr().err
    assert "failed to start the supervisor" in err
    assert "injected script write failure" in err
    states = [record.get("state") for record in roster().values()]
    assert states == ["failed"]
    assert "starting" not in states


def test_launch_for_a_front_with_no_record_is_refused_by_name(env, capsys):
    make_repo(env / "repo")
    assert cli.main(["launch", "supervisor", "nosuchfront", "--repo",
                     str(env / "repo"), "--dry-run"]) != 0
    err = capsys.readouterr().err
    assert "unknown front 'nosuchfront'" in err
    assert "no record on the ledger" in err
    assert roster() == {}


def test_launch_while_frozen_names_every_other_violation_too(env, capsys):
    """A refusal names every violated field at once, freeze included.

    The break this catches is the early return that reported the freeze and
    stopped, leaving three more refusals for the next attempt to discover.
    """
    make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    paths.ensure_state_tree()
    paths.frozen_path().write_text("owner froze it\n", encoding="utf-8")
    assert cli.main(["launch", "supervisor", "absent", "--workspace", "six",
                     "--repo", str(env / "does-not-exist")]) != 0
    err = capsys.readouterr().err
    assert "refuses while frozen" in err
    assert "unknown front 'absent'" in err
    assert "bad workspace" in err
    assert "is not a directory" in err


def test_bad_workspace_is_refused_by_name(env, capsys):
    make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--workspace", "six",
                     "--repo", str(env / "repo"), "--dry-run"]) != 0
    assert "bad workspace" in capsys.readouterr().err


def test_a_branch_other_than_the_checked_out_one_is_refused(env, capsys):
    """The branch in the prompt is the branch the session works on.

    A supervisor shares its checkout with whoever owns it, so the launcher
    refuses to name one branch and run on another rather than switching a
    checkout underneath its owner.
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--repo", str(repo),
                     "--branch", "nonexistent-branch", "--dry-run"]) != 0
    err = capsys.readouterr().err
    assert "'nonexistent-branch' is not the branch checked out" in err
    assert "'main'" in err
    assert roster() == {}

    # The branch that is checked out is accepted, and it is what the prompt
    # and the printed launch both name.
    assert cli.main(["launch", "supervisor", "panel", "--repo", str(repo),
                     "--branch", "main", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert line(out, "branch: ") == "main"
    prompt = Path(line(out, "role prompt: ")).read_text(encoding="utf-8")
    assert "- branch: main" in prompt


def test_the_window_helper_defaults_to_the_shipped_skill(env, monkeypatch):
    """Configurable, with the skills location as its default."""
    from foreman.pools import _common

    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    default = _common.window_launcher_or_default()
    assert default.endswith("/skills/muse-workers/launch-window.sh")
    monkeypatch.setenv("FOREMAN_WINDOW_LAUNCHER", "chosen-launcher")
    assert _common.window_launcher_or_default() == "chosen-launcher"


def test_the_generated_window_script_sleeps_after_the_vendor_returns(
        tmp_path):
    """The window closes itself, and not on the session's own last words.

    Takes no fixture on purpose: the shipped 120 seconds is what a real
    summon runs, and the break this catches is a generated script that
    drops the sleep, or sleeps before the vendor, while the constant in the
    module still reads 120. A fake `sleep` records the value and returns at
    once, so the ordering is proven without waiting for it.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    order = tmp_path / "order"
    for name in ("claude", "sleep"):
        script = bindir / name
        script.write_text(f'#!/bin/sh\nprintf "{name} %s\\n" "$*" >> {order}\n',
                          encoding="utf-8")
        os.chmod(script, 0o755)
    prompt = tmp_path / "role-prompt.md"
    prompt.write_text("the fresh role prompt\n", encoding="utf-8")

    inner = launch_module.supervisor_inner_command(
        pid_path=tmp_path / "pid", session_id="ses-linger",
        repo=str(tmp_path), role_prompt=prompt, vendor_id="vendor-uuid",
        resume=False)
    run = tmp_path / "run.sh"
    run.write_text("#!/bin/bash\n" + inner, encoding="utf-8")
    subprocess.run(["bash", str(run)], check=True,
                   env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                        "HOME": str(tmp_path)},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    lines = order.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("claude ")
    assert lines[-1] == "sleep 120"


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


def _checkpoint(sid: str, monkeypatch, capsys) -> None:
    monkeypatch.setenv(SESSION_ENV, sid)
    assert cli.main(["checkpoint",
                     "--doing", "Verifying the plugin skeleton job.",
                     "--next", "Plan the blocks task into three jobs.",
                     "--held", "muse worker on plugin skeleton"]) == 0
    monkeypatch.delenv(SESSION_ENV)
    capsys.readouterr()


def test_relaunch_replays_the_checkpoint_and_resumes_the_conversation(
        env, fakes, capsys, monkeypatch):
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    old = _summon(env, repo, capsys)
    vendor_id = (paths.session_dir(old) / "vendor-session").read_text(
        encoding="utf-8").strip()
    _checkpoint(old, monkeypatch, capsys)

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
    # one sits idle and never starts. The MCP flags name the successor's
    # own config, so the resumed session calls with its own row of verbs.
    vendor = fakes["claude_argv"].read_text(
        encoding="utf-8").strip().splitlines()
    assert vendor == ["--resume", vendor_id, "--model", "claude-opus-5",
                      "--dangerously-skip-permissions",
                      "--mcp-config",
                      str(paths.session_dir(new) / "mcp.json"),
                      "--strict-mcp-config"]
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


def test_the_fresh_prompt_reaches_the_resumed_session(
        env, fakes, capsys, monkeypatch):
    """A relaunch delivers its prompt, it does not merely write one.

    A `--resume` carries no positional prompt, so the delivery is the one a
    resumed conversation can act on: the prompt it already has names an
    absolute path, that path always holds the newest prompt for the front,
    and the window prints it. This test walks that chain the way the
    resumed session would.
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    old = _summon(env, repo, capsys)
    # What the first session actually received, from the fake vendor's argv.
    received = fakes["claude_argv"].read_text(encoding="utf-8")
    _checkpoint(old, monkeypatch, capsys)

    # The session reads its instruction: one absolute path to read on being
    # relaunched.
    told = re.search(r"^\s+(/\S+/supervisor-prompt\.md)$", received,
                     re.MULTILINE)
    assert told, "the summoned session was never told where to read a fresh prompt"
    fresh = Path(told.group(1))
    assert fresh == launch_module.front_prompt_path("panel")

    assert cli.main(["relaunch", old, "--workspace", "6"]) == 0
    out = capsys.readouterr().out
    new = line(out, "session: ")

    # Following that instruction now yields the successor's own prompt,
    # with the predecessor's checkpoint in it.
    delivered = fresh.read_text(encoding="utf-8")
    assert new in delivered
    assert f"Session {old}, which you replace" in delivered
    assert "Verifying the plugin skeleton job." in delivered
    assert delivered == (paths.session_dir(new) / "role-prompt.md").read_text(
        encoding="utf-8")
    # And the window says so on the way in, so the path is on screen too.
    assert str(fresh) in out
    assert "You were relaunched" in out
    assert str(fresh) in (paths.session_dir(new) / "run.sh").read_text(
        encoding="utf-8")


def test_relaunch_stops_the_predecessor_before_admitting_the_successor(
        env, fakes, capsys, monkeypatch):
    """Two live supervisors of one front is what this prevents.

    Marking the old record exited while its process runs on leaves a second
    session able to plan, dispatch and checkpoint on the same front, so the
    process is stopped first and the roster records what was stopped.
    """
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    old = _summon(env, repo, capsys)
    old_pid = roster()[old]["pid"]
    old_identity = roster()[old]["pid_starttime"]
    assert procs.same_process(old_pid, old_identity)

    assert cli.main(["relaunch", old, "--workspace", "6"]) == 0
    out = capsys.readouterr().out
    new = line(out, "session: ")
    assert line(out, "stopped: ").startswith(f"pid {old_pid}")

    # The predecessor's process is gone, not merely marked gone.
    assert not procs.same_process(old_pid, old_identity)
    record = roster()[old]
    assert record["state"] == "exited"
    assert record["stopped_pid"] == old_pid
    assert record["stopped_by"] == new
    assert record["stopped_at"]
    # One live supervisor on the front, and it is the successor.
    live = [sid for sid, entry in roster().items()
            if entry.get("state") == "running"]
    assert live == [new]
    assert launch_module.live_front_supervisor("panel") is not None
    assert launch_module.live_front_supervisor("panel")[0] == new


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
