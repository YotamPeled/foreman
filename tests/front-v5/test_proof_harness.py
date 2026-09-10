"""v5 proof harness: clause files under proof/v5, isolated world.

Each test drives ``python bin/foreman-proof`` as a subprocess with
FOREMAN_SESSION, FOREMAN_STATE and FOREMAN_CONFIG set to nonsense so a
harness that inherited them would not see the world it built. Cheap
commands are also compared against unset and an absent session id.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROOF = REPO / "bin" / "foreman-proof"

FOREMAN_VARS = ("FOREMAN_SESSION", "FOREMAN_STATE", "FOREMAN_CONFIG")
NONSENSE = {name: "nonsense" for name in FOREMAN_VARS}
ABSENT = {
    "FOREMAN_SESSION": "ses-absent",
    "FOREMAN_STATE": "absent-state",
    "FOREMAN_CONFIG": "absent-config",
}


def _env(foreman: dict[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    for name in FOREMAN_VARS:
        env.pop(name, None)
    if foreman:
        env.update(foreman)
    env["PYTHONPATH"] = str(REPO / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _run(*args: str, foreman: dict[str, str] | None = None,
         timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PROOF), *args],
        cwd=str(REPO),
        env=_env(foreman),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_proof(*args: str, timeout: float = 180,
              compare_env: bool = False) -> subprocess.CompletedProcess:
    """Run with FOREMAN_* = nonsense. Optionally assert unset/absent match."""
    nonsense = _run(*args, foreman=NONSENSE, timeout=timeout)
    if compare_env:
        unset = _run(*args, foreman=None, timeout=timeout)
        absent = _run(*args, foreman=ABSENT, timeout=timeout)
        for other, label in ((unset, "unset"), (absent, "absent id")):
            assert other.returncode == nonsense.returncode, (
                f"returncode differed with FOREMAN_* {label}: "
                f"{other.returncode} vs nonsense {nonsense.returncode}\n"
                f"nonsense stdout:\n{nonsense.stdout}\n"
                f"{label} stdout:\n{other.stdout}\n"
                f"nonsense stderr:\n{nonsense.stderr}\n"
                f"{label} stderr:\n{other.stderr}"
            )
            assert other.stdout == nonsense.stdout, (
                f"stdout differed with FOREMAN_* {label} vs nonsense:\n"
                f"{other.stdout!r}\nvs\n{nonsense.stdout!r}"
            )
    return nonsense


def _clause_lines(out: str, prefix: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith(prefix)]


def test_v5_list_prints_exactly_one_clause_0_1():
    proc = run_proof("v5", "--list", compare_env=True)
    assert proc.returncode == 0, proc.stderr
    lines = _clause_lines(proc.stdout, "CLAUSE 0.1")
    assert len(lines) == 1, proc.stdout
    assert " PASS" not in lines[0] and " FAIL" not in lines[0]
    assert proc.stdout.count("CLAUSE ") == 1


def test_v5_milestone_0_runs_and_exits_0():
    proc = run_proof("v5", "--milestone", "0")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    lines = _clause_lines(proc.stdout, "CLAUSE 0.1 PASS")
    assert len(lines) == 1, proc.stdout
    assert "/build/proof/" in lines[0]
    assert "/tmp/" not in lines[0]


def test_v5_unknown_milestone_exits_2_naming_proof_v5():
    proc = run_proof("v5", "--milestone", "9", compare_env=True)
    assert proc.returncode == 2, proc.stderr + proc.stdout
    combined = proc.stderr + proc.stdout
    assert "proof/v5" in combined
    assert _clause_lines(proc.stdout, "CLAUSE ") == []


def test_trial_frictions_break_12_still_fails():
    proc = run_proof("trial-frictions", "--break", "12", timeout=300)
    assert "CLAUSE 12 FAIL" in proc.stdout, proc.stdout + proc.stderr
