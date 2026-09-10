"""v5 proof breaks: --break runs a clause's own BREAK (decision 33).

Each test drives ``python bin/foreman-proof`` as a subprocess with
FOREMAN_SESSION, FOREMAN_STATE and FOREMAN_CONFIG set to nonsense so a
harness that inherited them would not see the world it built.
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


def _env() -> dict[str, str]:
    env = os.environ.copy()
    for name in FOREMAN_VARS:
        env.pop(name, None)
    env.update(NONSENSE)
    env["PYTHONPATH"] = str(REPO / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def run_proof(*args: str, timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PROOF), *args],
        cwd=str(REPO),
        env=_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_clause(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return directory


def test_break_0_1_red_and_restores_fake_binary(tmp_path):
    root = tmp_path / "world"
    proc = run_proof(
        "v5", "--milestone", "0", "--break", "0.1", "--root", str(root))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    lines = proc.stdout.splitlines()
    pass_lines = [ln for ln in lines if ln.startswith("CLAUSE 0.1 PASS")]
    assert len(pass_lines) == 1, proc.stdout
    red_lines = [ln for ln in lines if ln.startswith("BREAK 0.1 RED")]
    assert len(red_lines) == 1, proc.stdout
    fake = root / "bin" / "foreman"
    assert fake.is_file(), (
        f"fake foreman not restored at {fake}; "
        f"bin={list((root / 'bin').iterdir()) if (root / 'bin').is_dir() else 'missing'}\n"
        f"{proc.stdout}\n{proc.stderr}")
    assert not (root / "bin" / "foreman.broken").exists()


def test_break_survived_when_apply_changes_nothing(tmp_path):
    clauses = _write_clause(tmp_path / "clauses", "m0_survived.py", """\
CLAUSE = ("s.1", "noop apply")


def apply(world):
    return None


def restore(world):
    return None


BREAK = ("apply changes nothing", apply, restore)


def run(world):
    return "still green"
""")
    proc = run_proof("v5", "--clauses", str(clauses), "--break", "s.1")
    assert proc.returncode == 1, proc.stderr + proc.stdout
    survived = [ln for ln in proc.stdout.splitlines()
                if ln.startswith("BREAK s.1 SURVIVED")]
    assert len(survived) == 1, proc.stdout + proc.stderr


def test_break_without_BREAK_still_deliberately_fails(tmp_path):
    clauses = _write_clause(tmp_path / "clauses", "m0_nobreak.py", """\
CLAUSE = ("d.1", "no break tuple")


def run(world):
    return "would pass"
""")
    proc = run_proof("v5", "--clauses", str(clauses), "--break", "d.1")
    assert proc.returncode == 1, proc.stderr + proc.stdout
    fail_lines = [ln for ln in proc.stdout.splitlines()
                  if ln.startswith("CLAUSE d.1 FAIL")]
    assert len(fail_lines) == 1, proc.stdout + proc.stderr
    assert "deliberately broken" in fail_lines[0]


def test_break_not_green_when_clause_fails_first(tmp_path):
    clauses = _write_clause(tmp_path / "clauses", "m0_notgreen.py", """\
CLAUSE = ("n.1", "already failing")


def apply(world):
    return None


def restore(world):
    return None


BREAK = ("would rename the fake foreman", apply, restore)


def run(world):
    raise Failure("clause is already red")
""")
    proc = run_proof("v5", "--clauses", str(clauses), "--break", "n.1")
    assert proc.returncode == 1, proc.stderr + proc.stdout
    not_green = [ln for ln in proc.stdout.splitlines()
                 if ln.startswith("BREAK n.1 NOT-GREEN")]
    assert len(not_green) == 1, proc.stdout + proc.stderr
