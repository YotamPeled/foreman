"""Headless turns for supervisors and the desk.

Every test drives the real entry points against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``, inheriting no ``FOREMAN_*``
variable. A fake ``claude`` sits on PATH: it records its argv and prints
a stream-json transcript with a result line — no test here starts a real
vendor process. The systemd unit a turn runs under is never executed
here: the tests assert its argv structurally (``RuntimeMaxSec``) through
a ``spawn`` double that runs the turn's script itself for real.

The break each test catches: a launch that opens a window or demands a
workspace when asked for headless, a first turn without the prompt, the
MCP config or stream-json, a vendor id never recorded, a later turn that
does not resume, a timeout that never reaches the unit, a failed turn
that consumes its event or is never retried, a second failure with no
anomaly, a relaunch that starts a process instead of queueing a wake, a
``headless = true`` that changes nothing, and a ``--workspace`` a
headless session silently keeps.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from foreman import cli, paths, store
from foreman import headless as headless_module
from foreman import launch as launch_module
from foreman import wake as wake_module
from foreman.caller import SESSION_ENV
from foreman import mcp as mcp_module

ROOT = Path(__file__).resolve().parents[2]
PANEL_BRIEF = ROOT / "briefs" / "panel"

VENDOR_ID = "11111111-2222-4333-8444-555555555555"


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
def fake_claude(env, monkeypatch):
    """A fake `claude` on PATH: records argv, prints a result line.

    Knobs through the environment: ``FAKE_VENDOR`` (the session id the
    stream reports), ``FAKE_RESULT`` (``ok`` | ``is_error`` | ``none`` —
    the last prints no result line at all), ``FAKE_EXIT`` (exit status).
    Every invocation appends one ``===TURN===`` block to the argv log.
    """
    bindir = env / "bin"
    bindir.mkdir(exist_ok=True)
    argv_log = env / "claude-argv.log"
    argv_log.write_text("", encoding="utf-8")
    script = bindir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "===TURN===" >> "{argv_log}"\n'
        f'printf "%s\\n" "$@" >> "{argv_log}"\n'
        'echo "{\\"type\\":\\"system\\",\\"subtype\\":\\"init\\",'
        '\\"session_id\\":\\"$FAKE_VENDOR\\"}"\n'
        'if [ "$FAKE_RESULT" = "none" ]; then\n'
        '  echo "still thinking (no result line yet)";\n'
        'elif [ "$FAKE_RESULT" = "is_error" ]; then\n'
        '  echo "{\\"type\\":\\"result\\",\\"subtype\\":\\"error\\",'
        '\\"is_error\\":true,\\"num_turns\\":2,'
        '\\"usage\\":{\\"input_tokens\\":3,\\"output_tokens\\":4}}";\n'
        'else\n'
        '  echo "{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\",'
        '\\"is_error\\":false,\\"num_turns\\":3,'
        '\\"usage\\":{\\"input_tokens\\":10,\\"output_tokens\\":5}}";\n'
        'fi\n'
        'exit "$FAKE_EXIT"\n',
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_VENDOR", VENDOR_ID)
    monkeypatch.setenv("FAKE_RESULT", "ok")
    monkeypatch.setenv("FAKE_EXIT", "0")
    return {"bin": bindir, "argv_log": argv_log}


@pytest.fixture()
def fake_spawn(fake_claude):
    """A `spawn` double: asserts the unit shape, runs the script itself.

    The outer ``systemd-run`` argv is never executed (no test touches a
    systemd unit); its ``RuntimeMaxSec`` is asserted here on every call.
    The turn's script runs for real under ``bash`` with the fake
    ``claude`` first on PATH, so the transcript is a real execution.
    """

    calls: list[list[str]] = []

    def spawn(argv: list[str], *, timeout_s: float):
        calls.append(list(argv))
        assert argv[0] == "systemd-run", argv
        assert any(
            part.startswith("--property=RuntimeMaxSec=") for part in argv
        ), argv
        assert argv[-2:] == ["bash", argv[-1]], argv
        child_env = dict(os.environ)
        child_env["PATH"] = str(fake_claude["bin"]) + os.pathsep + os.environ[
            "PATH"
        ]
        return subprocess.run(
            ["bash", argv[-1]],
            capture_output=True,
            text=True,
            env=child_env,
            timeout=60,
        )

    spawn.calls = calls  # type: ignore[attr-defined]
    return spawn


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

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


def turns_of(sid: str) -> list[dict]:
    return headless_module.read_turns(sid)


def pending_of(sid: str) -> list[dict]:
    return wake_module.pending_events(sid)


def open_anomalies() -> list[dict]:
    try:
        records = store.read_ledger(paths.anomalies_path())
    except OSError:
        return []
    return [record for record in records
            if record.get("resolved_at") is None]


def argv_blocks(fake_claude) -> list[str]:
    return fake_claude["argv_log"].read_text(
        encoding="utf-8").split("===TURN===\n")[1:]


def launch_headless_supervisor(env, capsys, monkeypatch, fake_spawn,
                               *extra: str) -> str:
    """Summon a headless supervisor with the spawn double installed."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    monkeypatch.setattr(headless_module, "_default_spawn", fake_spawn)
    rc = cli.main(["launch", "supervisor", "panel", "--headless",
                   "--repo", str(repo), *extra])
    assert rc == 0, capsys.readouterr().err
    return line(capsys.readouterr().out, "session: ")


# --------------------------------------------------------------------------
# Both launch shapes roster a session with no window.
# --------------------------------------------------------------------------


def test_supervisor_headless_launch_rosters_with_no_window(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """Minted and rostered headless: no window, no workspace, no pid.

    The packaged workspace default is neutered, so a launch that still
    needed a workspace would refuse here instead of rostering.
    """
    monkeypatch.setattr(launch_module, "_packaged_defaults", dict)
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)

    record = roster()[sid]
    assert record["role"] == "supervisor"
    assert record["front"] == "panel"
    assert record["state"] == "running"
    assert record["headless"] is True
    assert record["pid"] is None
    assert record["vendor_session"] == VENDOR_ID
    assert (paths.session_dir(sid) / "vendor-session").read_text(
        encoding="utf-8").strip() == VENDOR_ID

    # The first turn ran once, through the fake: the role prompt as the
    # prompt, the session's MCP config, stream-json out, no --resume.
    blocks = argv_blocks(fake_claude)
    assert len(blocks) == 1
    first = blocks[0]
    assert first.splitlines()[0] == "-p"
    assert "supervisor of front panel" in first
    assert f"--mcp-config\n{paths.session_dir(sid) / 'mcp.json'}\n" in first
    assert "--strict-mcp-config" in first
    assert "--output-format\nstream-json\n" in first
    assert "--resume" not in first

    # One turn record carrying the result line, and the front names it.
    [turn] = turns_of(sid)
    assert turn["kind"] == "first"
    assert turn["is_error"] is False
    assert turn["num_turns"] == 3
    assert turn["usage"] == {"input_tokens": 10, "output_tokens": 5}
    from foreman import fronts

    assert fronts.read_front_record("panel")["supervisor"] == sid


def test_headless_launch_prints_no_window(env, fake_claude, fake_spawn,
                                          capsys, monkeypatch):
    """The report names no window and no process to watch."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    monkeypatch.setattr(headless_module, "_default_spawn", fake_spawn)
    assert cli.main(["launch", "supervisor", "panel", "--headless",
                     "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "window: none" in out
    assert "org.agent." not in out
    assert "AGENT_WS" not in out
    assert f"vendor session: {VENDOR_ID}" in out


def test_merge_desk_headless_launch_rosters_with_no_window(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """The desk mints headless the same way: no window, first turn now."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    monkeypatch.setattr(headless_module, "_default_spawn", fake_spawn)
    assert cli.main(["launch", "merge-desk", "--headless",
                     "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    sid = line(out, "session: ")
    record = roster()[sid]
    assert record["role"] == "merge-desk"
    assert record["headless"] is True
    assert record["state"] == "running"
    assert record["pid"] is None
    assert record["vendor_session"] == VENDOR_ID
    assert "window: none" in out
    prompt = (paths.session_dir(sid) / "role-prompt.md").read_text(
        encoding="utf-8")
    assert "merge desk" in prompt.lower()
    assert "end the turn" in prompt
    [turn] = turns_of(sid)
    assert turn["kind"] == "first"
    assert turn["is_error"] is False


def test_second_headless_supervisor_for_a_live_front_is_refused(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """One front, one supervisor — headless sessions hold the slot too."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    repo = str(make_repo(env / "repo-two"))
    capsys.readouterr()
    rc = cli.main(["launch", "supervisor", "panel", "--headless",
                   "--repo", repo])
    assert rc == 1
    assert f"already has a live supervisor '{sid}'" in \
        capsys.readouterr().err


def test_launch_foreman_headless_is_refused(env, capsys):
    """The foreman itself stays interactive: out of scope by name."""
    assert cli.main(["launch", "foreman", "--headless"]) == 1
    assert "stays interactive" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The first turn's argv, and the vendor id off its stream.
# --------------------------------------------------------------------------


def test_first_turn_argv_shape_is_prompt_mcp_and_stream_json(env):
    """The argv contract, without running anything."""
    vendor = headless_module.first_turn_vendor_argv(
        prompt_text="the role prompt",
        mcp_config=Path("/tmp/state/ses-x/mcp.json"))
    assert vendor[:3] == ["claude", "-p", "the role prompt"]
    assert "--resume" not in vendor
    assert "--mcp-config" in vendor
    assert "--strict-mcp-config" in vendor
    assert vendor[vendor.index("--output-format") + 1] == "stream-json"


def test_resume_turn_argv_resumes_with_the_event_text(env):
    """Later turns resume the recorded conversation with the event text."""
    vendor = headless_module.resume_turn_vendor_argv(
        vendor_id="vendor-uuid",
        event_text="the wake",
        mcp_config=Path("/tmp/state/ses-x/mcp.json"))
    assert vendor[:5] == ["claude", "-p", "--resume", "vendor-uuid",
                          "the wake"]
    assert vendor[vendor.index("--output-format") + 1] == "stream-json"
    assert "--mcp-config" in vendor


def test_run_script_carries_the_world_and_the_checkout(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """run.sh names the world and this checkout's package (rul-ym3xjam)."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    script = (paths.session_dir(sid) / "run.sh").read_text(encoding="utf-8")
    assert f"export FOREMAN_STATE=" in script
    assert str(env / "state") in script
    expected_src = ROOT / "src"
    assert (expected_src / "foreman" / "__init__.py").exists()
    assert str(expected_src) in script
    assert "PYTHONPATH" in script


def test_mcp_config_names_the_absolute_interpreter(env):
    """No bare `foreman` off PATH: interpreter plus checkout package."""
    path = mcp_module.write_mcp_config("ses-probe")
    server = json.loads(path.read_text(
        encoding="utf-8"))["mcpServers"]["foreman"]
    assert server["command"] == str(Path(sys.executable).resolve())
    assert server["command"] != "foreman"
    assert server["args"] == ["-m", "foreman", "mcp"]
    assert server["env"]["FOREMAN_SESSION"] == "ses-probe"
    expected_src = ROOT / "src"
    assert (expected_src / "foreman" / "__init__.py").exists()
    assert server["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(
        expected_src)


# --------------------------------------------------------------------------
# A later turn resumes the recorded id with the event text.
# --------------------------------------------------------------------------


def test_later_turn_resumes_vendor_id_with_event_text(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """The wake's own event ids become the prompt of the resumed turn."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    event = wake_module.append_event(
        sid, "job failed", job="job-0001", front="panel", task="tas-0001")
    capsys.readouterr()

    result = headless_module.run_wake(sid, spawn=fake_spawn)

    assert result["status"] == "ok"
    assert result["anomaly"] is False
    blocks = argv_blocks(fake_claude)
    assert len(blocks) == 2
    resumed = blocks[1]
    assert f"--resume\n{VENDOR_ID}\n" in resumed
    assert event["id"] in resumed
    assert "job failed" in resumed
    assert "--output-format\nstream-json\n" in resumed

    kinds = [turn["kind"] for turn in turns_of(sid)]
    assert kinds == ["first", "wake"]
    wake_turn = turns_of(sid)[1]
    assert wake_turn["events"] == [event["id"]]
    assert wake_turn["is_error"] is False
    assert wake_turn["num_turns"] == 3
    assert wake_turn["usage"] == {"input_tokens": 10, "output_tokens": 5}
    # Consumed: the wake is delivered exactly once.
    assert pending_of(sid) == []


def test_no_wake_runs_no_turn(env, fake_claude, fake_spawn, capsys,
                              monkeypatch):
    """Nothing queued means no process: a wake carries every turn."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    before = len(argv_blocks(fake_claude))
    result = headless_module.run_wake(sid, spawn=fake_spawn)
    assert result["status"] == "no-wake"
    assert len(argv_blocks(fake_claude)) == before
    assert len(turns_of(sid)) == 1


def test_turn_running_defers_the_wake(env, fake_claude, fake_spawn, capsys,
                                      monkeypatch):
    """A wake never lands while a turn of the session is running."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    wake_module.append_event(sid, "heartbeat")
    wake_module.turn_started(sid)
    try:
        result = headless_module.run_wake(sid, spawn=fake_spawn)
    finally:
        wake_module.turn_ended(sid)
    assert result["status"] == "deferred"
    assert len(argv_blocks(fake_claude)) == 1
    assert len(pending_of(sid)) == 1


# --------------------------------------------------------------------------
# The timeout reaches the unit as RuntimeMaxSec.
# --------------------------------------------------------------------------


def test_turn_timeout_reaches_the_unit_as_runtime_max_sec(env):
    """Default fifteen minutes; configured turn_timeout wins instead."""
    outer = headless_module.turn_outer_argv("ses-x", "/tmp/x/run.sh")
    assert "--property=RuntimeMaxSec=900" in outer
    assert outer[1] == "--user"
    assert "--unit=foreman-ses-x" in outer
    assert "--service-type=exec" in outer

    config = env / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "foreman.toml").write_text(
        '[launch]\nturn_timeout = "20m"\n', encoding="utf-8")
    assert headless_module.turn_timeout_text() == "20m"
    outer = headless_module.turn_outer_argv("ses-x", "/tmp/x/run.sh")
    assert "--property=RuntimeMaxSec=1200" in outer


def test_misshapen_turn_timeout_answers_the_default(env):
    """A timeout the unit cannot use is the default, never a crash."""
    config = env / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "foreman.toml").write_text(
        '[launch]\nturn_timeout = "soon"\n', encoding="utf-8")
    assert headless_module.turn_timeout_text() == "soon"
    assert headless_module.turn_timeout_seconds() == 900
    outer = headless_module.turn_outer_argv("ses-x", "/tmp/x/run.sh")
    assert "--property=RuntimeMaxSec=900" in outer


def test_timeout_expired_turn_is_retried_then_an_anomaly(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """A turn past its time limit dies the same death as exit non-zero."""

    def timeout_spawn(argv: list[str], *, timeout_s: float):
        raise subprocess.TimeoutExpired(argv, timeout_s)

    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    event = wake_module.append_event(sid, "heartbeat")

    result = headless_module.run_wake(sid, spawn=timeout_spawn)

    assert result["status"] == "failed"
    assert result["anomaly"] is True
    assert [turn["timed_out"] for turn in result["attempts"]] == [True, True]
    assert pending_of(sid) != []
    kinds = [anomaly["kind"] for anomaly in open_anomalies()
             if anomaly["subject"] == sid]
    assert headless_module.HEADLESS_ANOMALY in kinds


# --------------------------------------------------------------------------
# Failure: retried once with the same event, anomaly on the second.
# --------------------------------------------------------------------------


def test_failed_turn_retried_once_with_the_same_event(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """Two turns, one event text; the second failure is an anomaly."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    monkeypatch.setenv("FAKE_EXIT", "1")
    event = wake_module.append_event(
        sid, "job failed", job="job-0001", front="panel", task="tas-0001")

    result = headless_module.run_wake(sid, spawn=fake_spawn)

    assert result["status"] == "failed"
    assert result["anomaly"] is True
    assert len(result["attempts"]) == 2
    blocks = argv_blocks(fake_claude)
    assert len(blocks) == 3  # first turn plus the two wake attempts
    assert blocks[1].count(event["id"]) == 1
    assert blocks[2].count(event["id"]) == 1
    assert [turn["attempt"] for turn in turns_of(sid)[1:]] == [1, 2]
    assert [turn["exit_code"] for turn in turns_of(sid)[1:]] == [1, 1]
    kinds = [anomaly["kind"] for anomaly in open_anomalies()
             if anomaly["subject"] == sid]
    assert kinds == [headless_module.HEADLESS_ANOMALY]
    detail = [anomaly["detail"] for anomaly in open_anomalies()
              if anomaly["subject"] == sid][0]
    assert event["id"] in detail


def test_event_not_consumed_by_a_failed_turn(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """A failed turn leaves its event queued; success consumes it."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    monkeypatch.setenv("FAKE_EXIT", "1")
    event = wake_module.append_event(sid, "told", text="knock knock")

    assert headless_module.run_wake(sid, spawn=fake_spawn)["status"] \
        == "failed"
    queued = pending_of(sid)
    assert [record["id"] for record in queued] == [event["id"]]
    assert queued[0]["delivered_at"] is None

    monkeypatch.setenv("FAKE_EXIT", "0")
    assert headless_module.run_wake(sid, spawn=fake_spawn)["status"] == "ok"
    assert pending_of(sid) == []


def test_is_error_result_line_fails_the_turn_despite_exit_zero(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """The turn is judged by the result line and the exit, never prose."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    monkeypatch.setenv("FAKE_RESULT", "is_error")
    wake_module.append_event(sid, "heartbeat")
    result = headless_module.run_wake(sid, spawn=fake_spawn)
    assert result["status"] == "failed"
    assert result["anomaly"] is True
    assert result["attempts"][0]["result"]["is_error"] is True
    assert pending_of(sid) != []


def test_missing_result_line_fails_the_turn(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """No result line means no judgement, which reads as failure."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    monkeypatch.setenv("FAKE_RESULT", "none")
    wake_module.append_event(sid, "heartbeat")
    result = headless_module.run_wake(sid, spawn=fake_spawn)
    assert result["status"] == "failed"
    assert result["attempts"][0]["result"] is None
    assert turns_of(sid)[-1]["is_error"] is None
    assert pending_of(sid) != []


def test_first_turn_failed_twice_refuses_but_rosters(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """A dead summon leaves a record, never an orphan — and an anomaly."""
    monkeypatch.setenv("FAKE_EXIT", "1")
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    monkeypatch.setattr(headless_module, "_default_spawn", fake_spawn)
    rc = cli.main(["launch", "supervisor", "panel", "--headless",
                   "--repo", str(repo)])
    assert rc == 1
    assert "failed twice" in capsys.readouterr().err
    [sid] = list(roster())
    record = roster()[sid]
    assert record["headless"] is True
    assert record["state"] == "running"
    # The dead turns still reported a conversation: the next wake resumes
    # it instead of starting over.
    assert record["vendor_session"] == VENDOR_ID
    assert [turn["kind"] for turn in turns_of(sid)] == ["first", "first"]
    kinds = [anomaly["kind"] for anomaly in open_anomalies()
             if anomaly["subject"] == sid]
    assert kinds == [headless_module.HEADLESS_ANOMALY]


# --------------------------------------------------------------------------
# `relaunch` of a headless session is a wake.
# --------------------------------------------------------------------------


def test_relaunch_headless_is_a_wake_not_a_process(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """Queued as `told`: the roster, the vendor and the turns stand."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    turns_before = len(turns_of(sid))
    capsys.readouterr()

    assert cli.main(["relaunch", sid]) == 0
    out = capsys.readouterr().out
    assert "queued as a wake" in out

    queued = pending_of(sid)
    assert len(queued) == 1
    assert queued[0]["reason"] == "told"
    assert queued[0]["text"] == headless_module.RELAUNCH_TEXT
    assert roster()[sid]["pid"] is None
    assert roster()[sid]["vendor_session"] == VENDOR_ID
    assert len(turns_of(sid)) == turns_before


def test_relaunch_headless_dry_run_queues_nothing(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """A dry relaunch names the event text and writes no ledger line."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    capsys.readouterr()
    assert cli.main(["relaunch", sid, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert headless_module.RELAUNCH_TEXT in out
    assert pending_of(sid) == []


def test_relaunch_headless_with_workspace_refused_by_name(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """A headless session has no workspace, on relaunch as on launch."""
    sid = launch_headless_supervisor(
        env, capsys, monkeypatch, fake_spawn)
    capsys.readouterr()
    assert cli.main(["relaunch", sid, "--workspace", "6"]) == 1
    assert "--workspace" in capsys.readouterr().err
    assert pending_of(sid) == []


# --------------------------------------------------------------------------
# `headless = true` flips the default; `--workspace` needs a window.
# --------------------------------------------------------------------------


def test_headless_true_flips_the_default(env, fake_claude, fake_spawn,
                                         capsys, monkeypatch):
    """No flag, no workspace: the config alone summons headless."""
    config = env / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "foreman.toml").write_text(
        "[launch]\nheadless = true\n", encoding="utf-8")
    assert headless_module.headless_default() is True
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    monkeypatch.setattr(headless_module, "_default_spawn", fake_spawn)
    assert cli.main(["launch", "supervisor", "panel",
                     "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    sid = line(out, "session: ")
    assert roster()[sid]["headless"] is True
    assert "window: none" in out


def test_no_headless_keeps_the_windowed_shape(env, capsys):
    """The flag switches the default; it does not remove the window."""
    config = env / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "foreman.toml").write_text(
        "[launch]\nheadless = true\n", encoding="utf-8")
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--no-headless",
                     "--workspace", "6", "--repo", str(repo),
                     "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "window: org.agent.foreman-" in out
    assert roster() == {}


def test_headless_with_workspace_refused_by_name(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    """Asking a headless session for a workspace names `--workspace`."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    monkeypatch.setattr(headless_module, "_default_spawn", fake_spawn)
    rc = cli.main(["launch", "supervisor", "panel", "--headless",
                   "--workspace", "6", "--repo", str(repo)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--workspace" in err
    assert roster() == {}
    assert argv_blocks(fake_claude) == []


def test_headless_and_no_headless_together_is_refused(
        env, capsys, monkeypatch):
    """Two opposite flags is a refusal naming both, before anything."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    rc = cli.main(["launch", "supervisor", "panel", "--headless",
                   "--no-headless", "--repo", str(repo)])
    assert rc == 1
    assert "--headless" in capsys.readouterr().err
    assert roster() == {}


def test_interactive_stays_the_default(env):
    """Nothing in the config means windows, as before this task."""
    assert headless_module.headless_default() is False
    assert headless_module.turn_timeout_text() == "15m"
    assert headless_module.turn_timeout_seconds() == 900


# --------------------------------------------------------------------------
# The headless role-prompt variant: one event, act, checkpoint, end.
# --------------------------------------------------------------------------


def test_headless_supervisor_prompt_carries_the_turn_contract(
        env, capsys):
    """The turn ends with the event; state is ledgers and checkpoint."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--headless",
                     "--repo", str(repo), "--dry-run"]) == 0
    out = capsys.readouterr().out
    prompt = Path(line(out, "role prompt: ")).read_text(encoding="utf-8")
    assert "How a headless turn works" in prompt
    assert "end the turn" in prompt
    assert "the work of this event is done" in prompt
    assert "ledgers and your checkpoint" in prompt
    assert "keyboard" in prompt
    assert "supervisor of front panel" in prompt
    assert "{{" not in prompt


def test_windowed_prompt_has_no_headless_contract(env, capsys):
    """The variant changes the headless prompt, never the windowed one."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                     "--repo", str(repo), "--dry-run"]) == 0
    out = capsys.readouterr().out
    prompt = Path(line(out, "role prompt: ")).read_text(encoding="utf-8")
    assert "How a headless turn works" not in prompt
