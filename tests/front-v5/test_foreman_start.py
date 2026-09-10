"""`foreman start`: the collector unit and the foreman in one verb.

Every test runs the real entry point against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path`` with ``FOREMAN_SESSION`` unset
(or set to a seated session where the test is about a role). Two doors
are captured: ``collector.systemctl``, the one systemd function, and the
launcher's ``spawn_and_wait``, the last step before a real window. Both
doubles refuse any argv production would not hand them.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from foreman import cli, collector, entities, ids, paths, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.entities import Session
from foreman.pools import _common

UNIT = collector.COLLECTOR_UNIT
MODEL = "claude-fable-5-1"
EFFORT = "xhigh"


class Systemd:
    """Records every systemctl call; answers is-active from ``active``."""

    ACCEPTED = {("is-active", "--quiet", UNIT), ("daemon-reload",),
                ("enable", "--now", UNIT), ("start", UNIT)}

    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> tuple[int, str]:
        if args not in self.ACCEPTED:
            raise AssertionError(f"start/doctor may not run systemctl {args}")
        self.calls.append(args)
        if args[0] == "is-active":
            return (0 if self.active else 3), ""
        if args[0] in ("enable", "start"):
            self.active = True
        return 0, ""

    def changes(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] != "is-active"]


class Spawn:
    """The window launcher's spawn: writes the pid file, starts nothing.

    Refuses what the real spawn would fail on: a launcher that is not an
    executable file, or a run script that was never written.
    """

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def __call__(self, argv, *, pid_path: Path, session_id, popen) -> int:
        launcher = next(a for a in argv if not a.startswith(("env", "AGENT_WS")))
        if not os.access(launcher, os.X_OK):
            raise FileNotFoundError(launcher)
        script = Path(argv[-1])
        if argv[-2] != "bash" or not script.is_file():
            raise AssertionError(f"no run script to hand the window: {argv}")
        self.scripts.append(script.read_text(encoding="utf-8"))
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        return os.getpid()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(launch_module.VENDOR_CONFIG_ENV,
                       str(tmp_path / "vendor.json"))
    launcher = tmp_path / "window-launcher"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setenv(_common.WINDOW_LAUNCHER_ENV, str(launcher))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "test@example.invalid"),
                 ("config", "user.name", "test"),
                 ("commit", "-q", "--allow-empty", "-m", "seed")):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)
    return tmp_path


@pytest.fixture()
def systemd(monkeypatch):
    double = Systemd()
    monkeypatch.setattr(collector, "systemctl", double)
    return double


@pytest.fixture()
def spawn(monkeypatch):
    double = Spawn()
    monkeypatch.setattr(_common, "spawn_and_wait", double)
    return double


def start(env, *extra: str) -> int:
    return cli.main(["start", "--model", MODEL, "--effort", EFFORT,
                     "--repo", str(env / "repo"), *extra])


def roster() -> dict:
    return store.read_snapshot(paths.roster_path(),
                               default={"sessions": {}})["sessions"]


def test_first_start_writes_the_unit_enables_it_and_spawns_the_foreman(
        env, systemd, spawn, capsys):
    """Absent unit: written from render_unit, reloaded, enabled; the
    foreman is spawned with the model and effort; two lines printed."""
    assert start(env) == 0
    out = capsys.readouterr().out.splitlines()

    unit = collector.unit_path()
    assert unit.read_text(encoding="utf-8") == collector.render_unit()
    assert systemd.changes() == [("daemon-reload",), ("enable", "--now", UNIT)]
    assert len(spawn.scripts) == 1
    assert f"--model {MODEL} --effort {EFFORT} " in spawn.scripts[0]

    [(sid, record)] = [(k, v) for k, v in roster().items()
                       if v.get("role") == "foreman"]
    assert record["state"] == "running"
    assert (record["model"], record["effort"]) == (MODEL, EFFORT)
    assert out == [f"collector: wrote {unit}, enabled and started {UNIT}",
                   f"foreman: {sid} ({MODEL}, {EFFORT})"]


def test_a_second_start_is_refused_naming_the_live_foreman(
        env, systemd, spawn, capsys):
    """One foreman at a time: the second start names the live one and
    touches neither systemd nor the launcher."""
    assert start(env) == 0
    capsys.readouterr()
    [sid] = [k for k, v in roster().items() if v.get("role") == "foreman"]
    calls, scripts = list(systemd.calls), list(spawn.scripts)

    assert start(env) == 1
    captured = capsys.readouterr()
    assert f"a foreman is live: {sid}; stop it first" in captured.err
    assert captured.out == ""
    assert systemd.calls == calls
    assert spawn.scripts == scripts


def test_an_active_unit_is_left_alone(env, systemd, spawn, capsys):
    """Present and active: never rewritten, restarted or re-enabled."""
    unit = collector.unit_path()
    unit.parent.mkdir(parents=True)
    unit.write_text("# the owner's own unit\n", encoding="utf-8")
    systemd.active = True

    assert start(env) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "collector: running (left alone)"
    assert out[1].startswith("foreman: ses-")
    assert systemd.changes() == []
    assert unit.read_text(encoding="utf-8") == "# the owner's own unit\n"


def test_an_inactive_unit_is_started_not_rewritten(
        env, systemd, spawn, capsys):
    unit = collector.unit_path()
    unit.parent.mkdir(parents=True)
    unit.write_text("# the owner's own unit\n", encoding="utf-8")

    assert start(env) == 0
    assert capsys.readouterr().out.splitlines()[0] == \
        f"collector: started {UNIT}"
    assert systemd.changes() == [("start", UNIT)]
    assert unit.read_text(encoding="utf-8") == "# the owner's own unit\n"


def test_dry_run_writes_nothing_spawns_nothing_and_says_would(
        env, systemd, spawn, capsys):
    assert start(env, "--dry-run") == 0
    out = capsys.readouterr().out.splitlines()

    assert len(out) == 2
    assert all(line.startswith("would ") for line in out), out
    assert f"--model {MODEL} --effort {EFFORT}" in out[1]
    assert not collector.unit_path().exists()
    assert systemd.changes() == []
    assert spawn.scripts == []
    assert roster() == {}
    assert not paths.sessions_dir().exists() or \
        list(paths.sessions_dir().iterdir()) == []


@pytest.mark.parametrize("role", ["supervisor", "foreman", "opus"])
def test_every_session_role_is_refused(
        env, systemd, spawn, monkeypatch, capsys, role):
    """The owner starts the swarm from a terminal; no session does."""
    sid = "ses-caller01"
    store.write_snapshot(paths.roster_path(), {"sessions": {
        sid: Session(id=sid, role=role, pool="claude", model=MODEL,
                     front="alpha", state="exited").to_dict()}})
    monkeypatch.setenv(SESSION_ENV, sid)

    assert start(env) == 1
    assert f"role '{role}' may not call 'start'" in capsys.readouterr().err
    assert systemd.calls == []
    assert spawn.scripts == []
    assert not collector.unit_path().exists()


def test_doctor_names_the_unit_state_and_the_foreman(
        env, systemd, spawn, capsys):
    cli.main(["doctor"])
    before = capsys.readouterr().out.splitlines()
    assert f"doctor: collector unit {UNIT}: absent" in before
    assert "doctor: no foreman" in before

    assert start(env) == 0
    capsys.readouterr()
    [sid] = [k for k, v in roster().items() if v.get("role") == "foreman"]
    cli.main(["doctor"])
    after = capsys.readouterr().out.splitlines()
    assert f"doctor: collector unit {UNIT}: active" in after
    assert f"doctor: foreman: {sid} ({MODEL}, {EFFORT})" in after


def test_status_header_names_the_foreman_on_a_v5_swarm(
        env, systemd, spawn, capsys):
    paths.front_dir("alpha").mkdir(parents=True, exist_ok=True)
    store.append_ledger(paths.front_record_path("alpha"), entities.Front(
        id=ids.mint("front"), name="alpha", state="active", shape="v5",
        goal="Hold the swarm.", finish_line="It is held.").to_dict())
    cli.main(["status"])
    assert "\nforeman: none\n" in capsys.readouterr().out

    assert start(env) == 0
    capsys.readouterr()
    [sid] = [k for k, v in roster().items() if v.get("role") == "foreman"]
    cli.main(["status"])
    assert f"\nforeman: {sid} ({MODEL}, {EFFORT})\n" in \
        capsys.readouterr().out


def test_the_systemd_function_is_inert_on_an_isolated_world(
        env, real_door, monkeypatch):
    """No isolated world reaches the machine's user manager, even when a
    test forgets to replace the door."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"reached systemctl: {args!r}")

    monkeypatch.setattr(collector.subprocess, "run", refuse)
    code, said = real_door("foreman.collector.systemctl")("is-active",
                                                           "--quiet", UNIT)
    assert code == 1
    assert "isolated world" in said
