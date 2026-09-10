"""Every appended ledger line names the build that wrote it.

A proof run against a different checkout used to be indistinguishable
from one against the front's contracted tree. These tests fail on a
writer that skips the stamp, records a different interpreter, or names
a commit that is not this checkout's HEAD. Status fails if it does not
count and mark evidence whose build is not the front's base.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from foreman import cli, paths, store
from foreman.caller import SESSION_ENV
from foreman.status import NOW_ENV

ROOT = Path(__file__).resolve().parents[2]

BASE_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    return tmp_path


def checkout_head() -> str:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def test_append_ledger_stamps_the_running_build(env):
    """A line appended in a fresh world carries this interpreter and
    this checkout's HEAD."""
    entry = store.append_ledger(
        paths.inbox_path(), {"kind": "note", "text": "hello"})
    build = entry["build"]
    assert build["interpreter"] == sys.executable
    assert build["commit"] == checkout_head()
    assert build["package"] == str(Path(store.__file__).resolve().parent)
    assert store.read_ledger(paths.inbox_path()) == [entry]


def _jsonl(*records: dict) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


def test_evidence_prints_the_build_commit(env, monkeypatch, capsys):
    """`foreman evidence` names the commit it is writing with."""
    store.append_ledger(
        paths.front_record_path("probe"),
        {"id": "frn-probe", "name": "probe", "state": "active"})
    assert cli.main(["evidence", "--on", "probe", "--claim", "seen it",
                     "--status", "PLAUSIBLE"]) == 0
    out = capsys.readouterr().out
    assert "evidence on probe" in out
    assert f"build {checkout_head()}" in out


def test_status_marks_evidence_from_another_build(env, monkeypatch, capsys):
    """A line whose build is another sha is counted and marked; a line
    with no build prints as it always has."""
    paths.front_dir("probe").mkdir(parents=True, exist_ok=True)
    paths.front_record_path("probe").write_text(_jsonl({
        "id": "frn-probe",
        "name": "probe",
        "state": "active",
        "shape": "v5",
        "want": "Stamp the build.",
        "done_when": "Every line names its build.",
        "allocation": {},
        "repositories": [
            {"name": "foreman", "base": "main", "base_sha": BASE_SHA},
        ],
    }), encoding="utf-8")
    paths.front_evidence_path("probe").write_text(_jsonl(
        {
            "on": "probe",
            "claim": "green on another checkout",
            "status": "CONFIRMED",
            "command": "make check",
            "build": {
                "commit": OTHER_SHA,
                "interpreter": sys.executable,
                "package": "/opt/foreman",
            },
        },
        {
            "on": "probe",
            "claim": "old line with no build",
            "status": "CONFIRMED",
            "command": "make check",
        },
    ), encoding="utf-8")
    monkeypatch.setenv(NOW_ENV, "2026-09-08T12:00:00+00:00")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "evidence: 2 (2 confirmed, 1 from another build)" in out
    marker = f" \u00b7 build {OTHER_SHA[:7]} \u2260 base"
    other_lines = [line for line in out.splitlines()
                   if "green on another checkout" in line]
    assert other_lines
    assert all(marker in line for line in other_lines)
    old_lines = [line for line in out.splitlines()
                 if "old line with no build" in line]
    assert old_lines
    assert all("\u2260 base" not in line for line in old_lines)
