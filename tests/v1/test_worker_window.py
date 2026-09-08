"""No worker or reviewer is ever on screen.

Owner ruling, 2026-09-08: worker and reviewer agents never show a command
line on screen; only the foreman and supervisors are visible as command
lines. So a job launch has no window a supervisor can ask for. The flag
survives only as the owner's own debugging switch, and the refusals are
here rather than in a sentence in a spec, because a rule that lives in a
spec is one careless launch away from being gone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "v0"))

from test_launch import (  # noqa: E402
    SPEC_OK, env, fake_pool, launch, make_repo, write_spec,
)

from foreman import entities, paths, store  # noqa: E402
from foreman.caller import SESSION_ENV  # noqa: E402
from foreman.launch import WORKER_WINDOW_ENV  # noqa: E402


def roster_supervisor(sid="ses-sup0001", front="corpus"):
    store.write_snapshot(paths.roster_path(), {"sessions": {
        sid: entities.Session(id=sid, role="supervisor", pool="opus",
                              front=front, state="running").to_dict()}})
    return sid


def test_a_supervisor_asking_for_a_worker_window_is_refused(
        env, fake_pool, monkeypatch, capsys):
    """The refusal names the role and says where a window is allowed."""
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    monkeypatch.setenv(SESSION_ENV, roster_supervisor())
    monkeypatch.setenv(WORKER_WINDOW_ENV, "1")

    assert launch(["muse", "fake", spec, "--repo", str(repo), "--window"]) == 1
    err = capsys.readouterr().err
    assert "role 'supervisor' may not ask for a worker window" in err
    assert "never on screen" in err
    assert store.read_snapshot(paths.roster_path(), default={})[
        "sessions"].keys() == {"ses-sup0001"}


def test_the_owner_needs_the_switch_on(env, fake_pool, monkeypatch, capsys):
    """Even the owner does not get one by typing the flag alone."""
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    monkeypatch.delenv(WORKER_WINDOW_ENV, raising=False)

    assert launch(["muse", "fake", spec, "--repo", str(repo), "--window"]) == 1
    assert WORKER_WINDOW_ENV in capsys.readouterr().err


def test_the_owner_with_the_switch_on_may_watch_one(
        env, fake_pool, monkeypatch, capsys):
    """The debugging switch still works, or it is not a switch."""
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    monkeypatch.setenv(WORKER_WINDOW_ENV, "1")

    assert launch(["muse", "fake", spec, "--repo", str(repo), "--window"]) == 0


def test_a_launch_without_the_flag_is_never_refused_for_it(
        env, fake_pool, monkeypatch, capsys):
    """A supervisor's ordinary launch is untouched by any of this."""
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    monkeypatch.setenv(SESSION_ENV, roster_supervisor())

    assert launch(["muse", "fake", spec, "--repo", str(repo)]) == 0
