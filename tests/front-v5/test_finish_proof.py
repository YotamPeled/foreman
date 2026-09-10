"""Finish-line proof: four clause ids, listed after the milestone files."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROOF = REPO / "bin" / "foreman-proof"

FOREMAN_VARS = ("FOREMAN_SESSION", "FOREMAN_STATE", "FOREMAN_CONFIG")
NONSENSE = {name: "nonsense" for name in FOREMAN_VARS}

FINISH_IDS = ("f1", "f2", "f3", "f4")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    for name in FOREMAN_VARS:
        env.pop(name, None)
    env.update(NONSENSE)
    env["PYTHONPATH"] = str(REPO / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def test_live_unknown_front_refuses_without_writing(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    marker = state / "untouched"
    marker.write_text("ok\n", encoding="utf-8")
    before = marker.read_text(encoding="utf-8")
    os.chmod(state, 0o555)
    env = os.environ.copy()
    for name in FOREMAN_VARS:
        env.pop(name, None)
    env["FOREMAN_STATE"] = str(state)
    env["PYTHONPATH"] = str(REPO / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.run(
            [sys.executable, str(PROOF), "v5", "--live", "--front", "nosuch"],
            cwd=str(REPO),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        os.chmod(state, 0o755)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "nosuch" in combined
    assert "live: read-only" in proc.stdout
    assert marker.read_text(encoding="utf-8") == before
    assert {path.name for path in state.iterdir()} == {"untouched"}


def test_list_prints_the_four_finish_line_ids():
    proc = subprocess.run(
        [sys.executable, str(PROOF), "v5", "--list"],
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
    for cid in FINISH_IDS:
        assert cid in ids, proc.stdout
    last_four = tuple(ids[-4:])
    assert last_four == FINISH_IDS, proc.stdout
