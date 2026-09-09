"""Smoke tests reap the systemd units they start.

The ordinary suite never imports this to talk to systemd: the reaper
takes ``stop``/``reset``/``run`` so a test can double them, and the
census fixture in conftest is bound only to tests marked smoke.
"""
from __future__ import annotations

import subprocess
from subprocess import DEVNULL
from typing import Any


class SmokeUnitReaper:
    """Session ids a smoke test launched; teardown reaps each unit."""

    def __init__(self, *, stop=None, reset=None):
        from foreman.pools import _common

        self.ids: list[str] = []
        self.stop = stop if stop is not None else _common.stop_unit
        self.reset = reset if reset is not None else _common.reset_failed_unit

    def record(self, session_id: str | None) -> None:
        if session_id:
            self.ids.append(session_id)

    def teardown(self) -> None:
        ids, self.ids = self.ids, []
        for sid in ids:
            self.stop(sid)
            self.reset(sid)


def list_foreman_units(*, run: Any = None) -> set[str]:
    """This user's loaded ``foreman-*`` units, or raise if they cannot be listed."""
    runner = run if run is not None else subprocess.run
    try:
        proc = runner(
            ["systemctl", "--user", "list-units", "--all",
             "--plain", "--no-legend", "--no-pager", "foreman-*"],
            stdout=subprocess.PIPE, stderr=DEVNULL, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError("could not list user foreman units") from exc
    if proc.returncode != 0:
        raise AssertionError(
            f"systemctl list-units exited {proc.returncode}")
    units: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        if name.startswith("foreman-"):
            units.add(name)
    return units


def leftover_smoke_units(before: set[str], after: set[str]) -> set[str]:
    """Worker units a smoke run added and did not reap.

    A smoke launch is ``foreman-<session>`` (session ids start ``ses-``).
    The live collector's turn carriers (``foreman-turn-*``) and the
    collector unit itself are not this run's, so they are not leftovers.
    """
    def workers(units: set[str]) -> set[str]:
        return {name for name in units if name.startswith("foreman-ses-")}

    return workers(after) - workers(before)
