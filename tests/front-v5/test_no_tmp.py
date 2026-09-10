"""Scratch worktrees live under the state directory, never on /tmp."""

from __future__ import annotations

import pytest

from foreman import paths


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def test_scratch_worktree_dir_is_under_state(env):
    path = paths.scratch_worktree_dir("verify", "job-abc")
    assert path == paths.state_dir() / "worktrees" / "scratch" / "verify-job-abc"
    assert path.parent.is_dir()
    assert not path.exists()
    land = paths.scratch_worktree_dir("land", "mrg-xyz")
    assert land == paths.state_dir() / "worktrees" / "scratch" / "land-mrg-xyz"


def test_remove_scratch_tolerates_absence_and_deletes(env):
    path = paths.scratch_worktree_dir("verify", "job-gone")
    paths.remove_scratch(path)
    path.mkdir()
    (path / "marker").write_text("x", encoding="utf-8")
    paths.remove_scratch(path)
    assert not path.exists()
    paths.remove_scratch(path)
