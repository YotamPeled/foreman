"""Milestone 4 clause files are listed by id 4.1 through 4.5."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROOF = REPO / "bin" / "foreman-proof"

FOREMAN_VARS = ("FOREMAN_SESSION", "FOREMAN_STATE", "FOREMAN_CONFIG")
NONSENSE = {name: "nonsense" for name in FOREMAN_VARS}

M4_IDS = ("4.1", "4.2", "4.3", "4.4", "4.5")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    for name in FOREMAN_VARS:
        env.pop(name, None)
    env.update(NONSENSE)
    env["PYTHONPATH"] = str(REPO / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def test_milestone_4_list_prints_five_ids():
    proc = subprocess.run(
        [sys.executable, str(PROOF), "v5", "--milestone", "4", "--list"],
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
    assert tuple(sorted(ids)) == M4_IDS, proc.stdout
    assert len(ids) == 5, proc.stdout
