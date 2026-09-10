"""Decision 37: nothing Foreman runs may alter a repository it did not
create. Scratch repositories are made with `git init --bare <path>` from a
neutral working directory, never `git -C <root> init --bare <path>`; and
`foreman doctor` checks the install clone is still a work tree."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from foreman import collector, doctor
from foreman.caller import SESSION_ENV

REPO = Path(__file__).resolve().parents[2]

# A helper call such as git(root, "init", ..., "--bare", ...) runs
# `git -C root init --bare`: the pattern decision 37 forbids.
ROOTED_BARE_INIT = re.compile(
    r"""git\(\s*\w+\s*,\s*["']init["'][^)]*["']--bare["']""")
LITERAL_ROOTED = re.compile(r"""["']-C["']\s*,\s*[^,]+,\s*["']init["']""")

BUILT_BY_8_4A = pytest.mark.xfail(
    strict=True, reason="job-8.4a builds this; its worker removes this marker")
BUILT_BY_8_4B = pytest.mark.xfail(
    strict=True, reason="job-8.4b builds this; its worker removes this marker")


def _sources() -> list[Path]:
    files = [*REPO.joinpath("tests").rglob("*.py"),
             *REPO.joinpath("proof").rglob("*.py"),
             REPO / "bin" / "foreman-proof"]
    return [path for path in files
            if path.is_file() and path.name != Path(__file__).name]


@BUILT_BY_8_4A
def test_no_scratch_repository_is_initialised_from_a_rooted_git():
    offenders = []
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in (ROOTED_BARE_INIT, LITERAL_ROOTED):
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(REPO)}:{line}")
    assert offenders == [], offenders


def _git(*args: str) -> None:
    subprocess.run(["git", "-c", "user.email=test@example.invalid",
                    "-c", "user.name=foreman-test", *args],
                   check=True, capture_output=True)


@BUILT_BY_8_4B
def test_doctor_reports_an_install_clone_that_is_no_longer_a_work_tree(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    clone = tmp_path / "install-clone"
    _git("init", "-b", "main", "-q", str(clone))
    _git("-C", str(clone), "commit", "-q", "--allow-empty", "-m", "init")
    _git("-C", str(clone), "config", "core.bare", "true")
    monkeypatch.setattr(collector, "unit_state", lambda: "stubbed by the test")
    monkeypatch.setattr(doctor, "_invoked_as_installed_cli", lambda: True)
    monkeypatch.setattr(doctor, "_package_checkout", lambda: clone)
    rc = doctor.doctor_main()
    shown = "".join(capsys.readouterr())
    assert rc != 0, shown
    assert str(clone) in shown and "work tree" in shown, shown
