"""Every appended ledger line names the build that wrote it.

A proof run against a different checkout used to be indistinguishable
from one against the front's contracted tree. These tests fail on a
writer that skips the stamp, records a different interpreter, or names
a commit that is not this checkout's HEAD.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from foreman import paths, store
from foreman.caller import SESSION_ENV

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    return tmp_path


def checkout_head() -> str:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def test_append_ledger_stamps_the_running_build(env):
    """A line appended in a fresh world carries this interpreter and
    this checkout's HEAD."""
    entry = store.append_ledger(
        paths.inbox_path(), {"kind": "note", "text": "hello"})
    build = entry["build"]
    assert build["interpreter"] == sys.executable
    assert build["commit"] == checkout_head()
    assert build["package"] == str(Path(store.__file__).resolve().parent)
    assert store.read_ledger(paths.inbox_path()) == [entry]
