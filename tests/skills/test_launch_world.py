"""The world travels with the session the launcher summons.

A summoned session takes both its identity and its state directory from
the environment, and only the identity was ever carried into the script
the launcher writes. Run against an isolated ``FOREMAN_STATE``, the
launcher summoned a supervisor into the *default* state directory, where
the id it had just minted does not exist: the session's first act was to
have its MCP server refused as an unregistered writer, recorded on the
real ledger as an anomaly. These tests pin the carry and the sentence
that says which world a session will read.
"""

from __future__ import annotations

import os

from foreman import launch, paths
from foreman.pools import _common


def test_script_carries_both_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.STATE_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(paths.CONFIG_ENV, str(tmp_path / "config"))
    script = _common.write_worker_script(tmp_path / "run.sh", "true\n")
    text = script.read_text(encoding="utf-8")
    assert f"export {paths.STATE_ENV}=" in text
    assert f"export {paths.CONFIG_ENV}=" in text
    assert str(tmp_path / "state") in text
    assert str(tmp_path / "config") in text


def test_the_world_is_exported_before_the_session_runs(tmp_path, monkeypatch):
    """Order matters: an export after the vendor line carries nothing."""
    monkeypatch.setenv(paths.STATE_ENV, str(tmp_path / "state"))
    monkeypatch.delenv(paths.CONFIG_ENV, raising=False)
    script = _common.write_worker_script(tmp_path / "run.sh", "the-vendor\n")
    text = script.read_text(encoding="utf-8")
    assert text.index(f"export {paths.STATE_ENV}=") < text.index("the-vendor")


def test_a_default_world_leaves_the_script_unchanged(tmp_path, monkeypatch):
    """A normal launch writes exactly the script it wrote before."""
    monkeypatch.delenv(paths.STATE_ENV, raising=False)
    monkeypatch.delenv(paths.CONFIG_ENV, raising=False)
    script = _common.write_worker_script(tmp_path / "run.sh", "true\n")
    assert script.read_text(encoding="utf-8") == "#!/bin/bash\ntrue\n\n"


def test_a_supervisor_script_carries_the_world(tmp_path, monkeypatch):
    """The supervisor shape, end to end through its own inner command."""
    monkeypatch.setenv(paths.STATE_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(paths.CONFIG_ENV, str(tmp_path / "config"))
    inner = launch.supervisor_inner_command(
        pid_path=tmp_path / "pid", session_id="ses-test", repo=str(tmp_path),
        role_prompt=tmp_path / "role-prompt.md", vendor_id="vendor-uuid")
    script = _common.write_worker_script(tmp_path / "run.sh", inner)
    text = script.read_text(encoding="utf-8")
    assert f"export {paths.STATE_ENV}={str(tmp_path / 'state')}" in text
    assert "export FOREMAN_SESSION=ses-test" in text
    assert text.index(paths.STATE_ENV) < text.index("claude")


def test_launch_output_names_a_non_default_world(tmp_path, monkeypatch,
                                                 capsys):
    monkeypatch.setenv(paths.STATE_ENV, str(tmp_path / "state"))
    monkeypatch.delenv(paths.CONFIG_ENV, raising=False)
    launch.print_world()
    out = capsys.readouterr().out
    assert str(tmp_path / "state") in out
    assert "not" in out and "default" in out
    assert paths.CONFIG_ENV not in out


def test_launch_output_is_silent_on_the_default_world(monkeypatch, capsys):
    monkeypatch.delenv(paths.STATE_ENV, raising=False)
    monkeypatch.delenv(paths.CONFIG_ENV, raising=False)
    launch.print_world()
    assert capsys.readouterr().out == ""
