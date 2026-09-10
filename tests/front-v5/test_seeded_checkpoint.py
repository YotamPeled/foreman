"""A summoned supervisor has a checkpoint before it has a process.

Every test drives the real entry points against a fresh FOREMAN_STATE
and FOREMAN_CONFIG. The windowed path uses the same fake window launcher
shape as tests/v1/test_summon.py, and that launcher fails if
checkpoint.json is missing at the moment the process starts. Headless
summon does the same through the spawn double. Nothing here starts a
real vendor process or writes a real state directory.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, paths, procs, store
from foreman import headless as headless_module
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.status import NOW_ENV

ROOT = Path(__file__).resolve().parents[2]
PANEL_BRIEF = ROOT / "briefs" / "panel"

SEEDED_DOING = "orienting: reading the role prompt and the front"
SEEDED_NEXT = "first checkpoint from the session itself"

NOW = datetime(2026, 9, 10, 12, 3, 0, tzinfo=timezone.utc)
SEEDED_AT = NOW - timedelta(minutes=3)
SUP = "ses-sup0001"
VENDOR_ID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(launch_module.VENDOR_CONFIG_ENV,
                       str(tmp_path / "home" / "vendor.json"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.setattr(launch_module, "WINDOW_LINGER_SECONDS", 5)
    yield tmp_path
    _kill_windows(tmp_path)


def _kill_windows(root: Path) -> None:
    sessions = store.read_snapshot(root / "state" / "roster.json",
                                   default={"sessions": {}})
    for record in (sessions.get("sessions") or {}).values():
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
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
    """Fake claude and a window launcher that demands checkpoint.json."""
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
        f"test -f {state}/sessions/$sid/checkpoint.json || {{ "
        'echo "checkpoint.json missing at process start" >&2; exit 1; }\n'
        f"cp {state}/sessions/$sid/checkpoint.json "
        f"{seen}/checkpoint.json\n"
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


@pytest.fixture()
def fake_claude(env, monkeypatch):
    bindir = env / "bin"
    bindir.mkdir(exist_ok=True)
    argv_log = env / "claude-argv.log"
    argv_log.write_text("", encoding="utf-8")
    script = bindir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "===TURN===" >> "{argv_log}"\n'
        f'printf "%s\\n" "$@" >> "{argv_log}"\n'
        'case " $* " in\n'
        '  *" --output-format stream-json "*)\n'
        '    case " $* " in\n'
        '      *" --verbose "*) : ;;\n'
        '      *) echo "Error: When using --print, --output-format=stream-json'
        ' requires --verbose" >&2; exit 1 ;;\n'
        '    esac ;;\n'
        'esac\n'
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
    return {"bin": bindir, "argv_log": argv_log}


@pytest.fixture()
def fake_spawn(fake_claude):
    calls: list[list[str]] = []
    seen: list[dict] = []

    def spawn(argv: list[str], *, timeout_s: float):
        calls.append(list(argv))
        assert argv[0] == "systemd-run", argv
        unit = next(part.split("=", 1)[1] for part in argv
                    if part.startswith("--unit="))
        assert unit.startswith("foreman-"), argv
        sid = unit[len("foreman-"):]
        path = paths.checkpoint_path(sid)
        assert path.is_file(), (
            f"checkpoint.json missing at first turn for {sid}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        seen.append(payload)
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
    spawn.seen = seen  # type: ignore[attr-defined]
    return spawn


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


def assert_seeded(payload: dict, sid: str) -> None:
    assert payload["seeded"] is True
    assert payload["doing"] == SEEDED_DOING
    assert payload["next"] == SEEDED_NEXT
    assert payload["by"] == "launcher"
    assert payload["session"] == sid
    assert isinstance(payload.get("at"), str) and payload["at"]


def test_windowed_summon_seeds_checkpoint_before_the_process(
        env, fakes, capsys):
    """The fake launcher reads checkpoint.json at its own entry."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()

    rc = cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                   "--repo", str(repo)])
    assert rc == 0, capsys.readouterr().err
    sid = line(capsys.readouterr().out, "session: ")

    at_entry = json.loads(
        (fakes["seen"] / "checkpoint.json").read_text(encoding="utf-8"))
    assert_seeded(at_entry, sid)
    assert_seeded(store.read_snapshot(paths.checkpoint_path(sid)), sid)


def test_headless_summon_seeds_checkpoint_before_the_process(
        env, fake_claude, fake_spawn, capsys, monkeypatch):
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    monkeypatch.setattr(headless_module, "_default_spawn", fake_spawn)

    rc = cli.main(["launch", "supervisor", "panel", "--headless",
                   "--repo", str(repo)])
    assert rc == 0, capsys.readouterr().err
    sid = line(capsys.readouterr().out, "session: ")

    assert len(fake_spawn.seen) == 1
    assert_seeded(fake_spawn.seen[0], sid)
    assert_seeded(store.read_snapshot(paths.checkpoint_path(sid)), sid)


def test_relaunch_seeds_checkpoint_before_the_process(
        env, fakes, capsys, monkeypatch):
    """A session's own checkpoint is overwritten by the relaunch seed."""
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                     "--repo", str(repo)]) == 0
    sid = line(capsys.readouterr().out, "session: ")

    monkeypatch.setenv(SESSION_ENV, sid)
    assert cli.main(["checkpoint", "--doing", "Planning the first job",
                     "--next", "Dispatch it"]) == 0
    monkeypatch.delenv(SESSION_ENV)
    capsys.readouterr()
    own = store.read_snapshot(paths.checkpoint_path(sid))
    assert own.get("seeded") is not True
    assert own["doing"] == "Planning the first job"

    assert cli.main(["relaunch", sid, "--workspace", "6"]) == 0
    capsys.readouterr()

    at_entry = json.loads(
        (fakes["seen"] / "checkpoint.json").read_text(encoding="utf-8"))
    assert_seeded(at_entry, sid)
    assert_seeded(store.read_snapshot(paths.checkpoint_path(sid)), sid)


def test_dry_run_writes_no_checkpoint(env, capsys, monkeypatch):
    repo = make_repo(env / "repo")
    add_panel_front()
    capsys.readouterr()

    assert cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                     "--repo", str(repo), "--dry-run"]) == 0
    windowed = line(capsys.readouterr().out, "session: ")
    assert not paths.checkpoint_path(windowed).exists()

    monkeypatch.setattr(launch_module, "_packaged_defaults", dict)
    assert cli.main(["launch", "supervisor", "panel", "--headless",
                     "--repo", str(repo), "--dry-run"]) == 0
    headless = line(capsys.readouterr().out, "session: ")
    assert not paths.checkpoint_path(headless).exists()


def _front_with_supervisor(env, monkeypatch, capsys, name: str = "plain"):
    subprocess.run(["git", "init", "-b", "main", "-q", str(env)], check=True)
    subprocess.run(["git", "-C", str(env), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    brief = env / name
    brief.mkdir()
    (brief / "brief.toml").write_text(
        f'name      = "{name}"\n'
        'order     = 1\n'
        'want      = "Something the owner asked for in plain words."\n'
        'done-when = "The thing works end to end on this machine."\n'
        'land-on   = "main"\n'
        'reviews   = "on request"\n'
        "after     = []\n"
        "prefer    = 0\n"
        "\n"
        "[allocation]\n"
        "muse = 1\n"
        "\n"
        "[[task]]\n"
        'title = "first"\n'
        'scope = """\n'
        "WHAT: do the first thing.\n"
        "INPUTS: the brief.\n"
        "OUTPUTS: the artifact.\n"
        "OUT OF SCOPE: everything else.\n"
        '"""\n'
        'verify = "make check first"\n'
        "size = 1\n"
        "after = []\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["front", "add", str(brief)]) == 0
    capsys.readouterr()
    store.write_snapshot(paths.roster_path(), {"sessions": {
        SUP: entities.Session.from_dict({
            "id": SUP, "role": "supervisor", "pool": "opus", "model": "opus",
            "front": name, "job": None, "pid": None, "pgid": None,
            "worktree": "", "log": "", "timeout": "20m",
            "launched_by": "owner",
            "started_at": SEEDED_AT.isoformat(),
            "last_declared_at": None, "last_observed_at": None,
            "cpu_s": 0.0, "state": "running",
        }).to_dict()}})


def test_status_renders_seeded_line_until_the_session_checkpoints(
        env, monkeypatch, capsys):
    _front_with_supervisor(env, monkeypatch, capsys)
    payload = entities.Checkpoint(
        session=SUP, doing=SEEDED_DOING, next=SEEDED_NEXT,
    ).to_dict()
    payload["by"] = "launcher"
    payload["seeded"] = True
    payload["at"] = SEEDED_AT.isoformat()
    store.write_snapshot(paths.checkpoint_path(SUP), payload)

    monkeypatch.setenv(NOW_ENV, NOW.isoformat())
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "orienting (seeded 3m ago)" in out
    assert "doing now:" not in out
    assert "doing now \u2014" not in out

    monkeypatch.setenv(SESSION_ENV, SUP)
    assert cli.main(["checkpoint", "--doing", "Planning the first job",
                     "--next", "Dispatch it"]) == 0
    capsys.readouterr()
    own = store.read_snapshot(paths.checkpoint_path(SUP))
    assert own.get("seeded") is not True
    assert own["doing"] == "Planning the first job"

    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "orienting (seeded" not in out
    assert "doing now: Planning the first job" in out
    assert "doing now \u2014 Planning the first job" in out
