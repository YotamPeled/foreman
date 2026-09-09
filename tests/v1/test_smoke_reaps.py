"""Smoke tests reap every unit they start.

The real vendor smoke test is the exception that reaches systemd. These
tests drive the same reaper with doubles, so a finished launch is still
stopped — the path that used to leave the unit loaded.
"""
from __future__ import annotations

import subprocess

import pytest

from foreman_test_smoke_units import leftover_smoke_units, list_foreman_units


def test_a_finished_smoke_launch_still_stops_its_unit(smoke_units):
    """A smoke launch that finished still leaves a unit loaded.

    The hang path already killed the process group; success did not.
    Fails if teardown only runs when the wait times out, or if it
    never calls the unit-stop helper.
    """
    calls: list[tuple[str, str]] = []
    smoke_units.stop = lambda sid: calls.append(("stop", sid)) or True
    smoke_units.reset = lambda sid: calls.append(("reset", sid)) or True
    smoke_units.record("ses-smokefin")
    smoke_units.teardown()
    assert calls == [("stop", "ses-smokefin"), ("reset", "ses-smokefin")]


def test_reaper_without_a_session_stops_nothing(smoke_units):
    """No recorded id means no systemctl. Fails if teardown stops a
    pattern or a hard-coded unit name."""
    calls: list[str] = []
    smoke_units.stop = lambda sid: calls.append(sid) or True
    smoke_units.reset = lambda sid: calls.append(sid) or True
    smoke_units.teardown()
    assert calls == []


def test_census_lists_this_users_foreman_units():
    """The leftover check asks the user manager for ``foreman-*``, not
    PID 1 and not a process-table grep. Fails if the pattern or the
    manager is wrong; unrelated unit names are not counted."""
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = (
            "foreman-ses-aaa.service loaded inactive dead\n"
            "ssh-agent.service loaded active running\n"
            "foreman-collector.service loaded active running\n"
        )

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Result()

    units = list_foreman_units(run=fake_run)
    assert calls == [[
        "systemctl", "--user", "list-units", "--all",
        "--plain", "--no-legend", "--no-pager", "foreman-*",
    ]]
    assert units == {
        "foreman-ses-aaa.service",
        "foreman-collector.service",
    }
    assert "ssh-agent.service" not in units


def test_census_fails_when_systemctl_cannot_be_asked():
    """A listing error is a failed check, not an empty set that would
    hide leftovers. Fails if a broken list-units is treated as 'none'."""

    class Result:
        returncode = 1
        stdout = ""

    with pytest.raises(AssertionError, match="exited 1"):
        list_foreman_units(run=lambda *a, **k: Result())

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("systemctl", 10)

    with pytest.raises(AssertionError, match="could not list"):
        list_foreman_units(run=boom)


def test_census_treats_a_left_worker_unit_as_a_leftover():
    """The leftover check is the worker unit a smoke launch starts.
    Fails if a finished ``foreman-ses-*`` unit is not counted."""
    before = {"foreman-collector.service"}
    after = {
        "foreman-collector.service",
        "foreman-ses-smokefin.service",
    }
    assert leftover_smoke_units(before, after) == {
        "foreman-ses-smokefin.service",
    }


def test_census_does_not_blame_the_collectors_turn_carriers():
    """A live collector tick during smoke is not a unit this run started.
    Fails if ``foreman-turn-*`` growth is treated as a leftover."""
    before = {
        "foreman-collector.service",
        "foreman-ses-already1.service",
    }
    after = before | {"foreman-turn-ses-other-1.service"}
    assert leftover_smoke_units(before, after) == set()
