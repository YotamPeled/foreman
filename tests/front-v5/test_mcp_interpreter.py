"""The written MCP config names an interpreter that can import foreman.

A launch whose ``sys.executable`` cannot import the package used to write
that interpreter into ``mcp.json`` anyway; Claude then started a server
that died on import and every verb went through the shell. These tests
fail on a writer that skips the probe, that does not fall back to an
installed ``foreman``, or that writes a config a real server cannot start
from.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from foreman import cli
from foreman import mcp as mcp_module
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV

ROOT = Path(__file__).resolve().parents[2]
PANEL_BRIEF = ROOT / "briefs" / "panel"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    return tmp_path


def write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def stub_interpreter(tmp_path: Path) -> Path:
    """A stand-in for ``sys.executable`` that fails ``import foreman``."""
    return write_exec(
        tmp_path / "bad-python",
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ] && [ "$2" = "import foreman" ]; then\n'
        "  echo \"ModuleNotFoundError: No module named 'foreman'\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 1\n",
    )


def fake_foreman(bindir: Path, *, succeed: bool) -> Path:
    """A ``foreman`` on PATH; ``succeed`` is the probe's exit."""
    bindir.mkdir(parents=True, exist_ok=True)
    rc = 0 if succeed else 1
    return write_exec(
        bindir / "foreman",
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ] && [ "$2" = "import foreman" ]; then\n'
        "  exit %d\n"
        "fi\n"
        "exit %d\n" % (rc, rc),
    )


def written_server(session_id: str) -> dict:
    path = mcp_module.write_mcp_config(session_id)
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["foreman"]


def line(out: str, prefix: str) -> str:
    for row in out.splitlines():
        if row.startswith(prefix):
            return row.split(prefix, 1)[1].strip()
    raise AssertionError(f"no {prefix!r} line in output:\n{out}")


def test_fallback_to_installed_foreman_when_interpreter_cannot_import(
        env, monkeypatch):
    """A dead ``sys.executable`` must not be written; the PATH command is.

    Fails on a writer that still names the stub interpreter, or that
    writes ``-m foreman mcp`` against a binary that is not an interpreter.
    """
    stub = stub_interpreter(env)
    fake = fake_foreman(env / "bin", succeed=True)
    monkeypatch.setattr(sys, "executable", str(stub))
    monkeypatch.setenv("PATH", str(env / "bin"))

    server = written_server("ses-fallback")
    assert server["command"] == str(fake)
    assert server["args"] == ["mcp"]


def test_both_candidates_failing_raises_refused_naming_both(env, monkeypatch):
    """No config is written when nothing probed can import the package.

    Fails on a writer that swallows the miss or whose refusal does not
    name both commands the owner would have to fix.
    """
    stub = stub_interpreter(env)
    fake = fake_foreman(env / "bin", succeed=False)
    monkeypatch.setattr(sys, "executable", str(stub))
    monkeypatch.setenv("PATH", str(env / "bin"))

    with pytest.raises(launch_module.Refused) as caught:
        mcp_module.write_mcp_config("ses-dead")
    message = str(caught.value)
    assert str(stub.resolve()) in message
    assert str(fake) in message
    assert not mcp_module.mcp_config_path("ses-dead").exists()


def test_real_interpreter_config_starts_a_server_that_lists_tools(env):
    """The config this checkout writes is one a server can actually start.

    Fails on a probe that picks a command whose ``mcp`` entry cannot
    answer ``tools/list``.
    """
    path = mcp_module.write_mcp_config("ses-real")
    server = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][
        "foreman"]
    interpreter = str(Path(sys.executable).resolve())
    assert server["command"] == interpreter
    assert server["args"] == ["-m", "foreman", "mcp"]

    child_env = os.environ.copy()
    child_env.update(server["env"])
    transport = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    proc = subprocess.run(
        [server["command"], *server["args"]],
        input="".join(json.dumps(line) + "\n" for line in transport),
        capture_output=True, text=True, timeout=60, env=child_env,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies) >= 2
    tools = replies[1]["result"]["tools"]
    assert isinstance(tools, list)
    assert any(tool["name"] == "version" for tool in tools)


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


def test_summon_prints_which_interpreter_was_probed(env, capsys):
    """The roster's owner sees which command the probe accepted.

    Fails on a summon that writes the config silently, so a dead
    interpreter is only discovered when Claude fails to start the server.
    """
    repo = make_repo(env / "repo")
    assert cli.main(["front", "add", str(PANEL_BRIEF)]) == 0
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                     "--repo", str(repo), "--dry-run"]) == 0
    out = capsys.readouterr().out
    path = mcp_module.mcp_config_path(line(out, "session: "))
    command = json.loads(path.read_text(encoding="utf-8"))[
        "mcpServers"]["foreman"]["command"]
    assert f"mcp: {command} (probed)" in out.splitlines()
