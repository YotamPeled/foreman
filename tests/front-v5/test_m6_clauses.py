"""Milestone 6 clause files are listed by id 6.1 through 6.3."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROOF = REPO / "bin" / "foreman-proof"

FOREMAN_VARS = ("FOREMAN_SESSION", "FOREMAN_STATE", "FOREMAN_CONFIG")
NONSENSE = {name: "nonsense" for name in FOREMAN_VARS}

M6_IDS = ("6.1", "6.2", "6.3")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    for name in FOREMAN_VARS:
        env.pop(name, None)
    env.update(NONSENSE)
    env["PYTHONPATH"] = str(REPO / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def test_milestone_6_list_prints_three_ids():
    proc = subprocess.run(
        [sys.executable, str(PROOF), "v5", "--milestone", "6", "--list"],
        cwd=str(REPO),
        env=_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    ids = []
    for line in proc.stdout.splitlines():
        if not line.startswith("CLAUSE "):
            continue
        parts = line.split(None, 2)
        assert len(parts) >= 2, line
        ids.append(parts[1])
    assert tuple(sorted(ids)) == M6_IDS, proc.stdout
    assert len(ids) == 3, proc.stdout
