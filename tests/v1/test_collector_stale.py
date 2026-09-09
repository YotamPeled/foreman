"""A collector older than the code it runs says so.

Every test drives the real ``tick`` (and the real ``status`` render) against
a fresh state directory, with the staleness check pointed at a fake package
checkout under tmp_path so no test touches the real sources. The git head
still comes from a real ``git`` call and the mtimes from real ``stat`` calls:
only the checkout location is redirected, never the comparison logic.

The break each test catches is in its docstring.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, collector, paths, store
from foreman.caller import SESSION_ENV
from foreman.collector import record_startup_version, tick
from foreman.status import NOW_ENV

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = "tests/v0/fixture"
PINNED_NOW = "2026-09-08T12:00:00+00:00"


def iso(moment: datetime) -> str:
    return moment.isoformat()


def later(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    config = tmp_path / "config" / "foreman.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[collector]\nvendor_markers = ["foreman-test-probe"]\n',
                      encoding="utf-8")
    return tmp_path


@pytest.fixture()
def code_checkout(tmp_path, monkeypatch):
    """A fake package checkout the staleness check reads instead of the
    real one: ``src/foreman/*.py`` plus a checkout root for the git head."""
    root = tmp_path / "checkout"
    package = root / "src" / "foreman"
    package.mkdir(parents=True)
    (package / "collector.py").write_text("STAMP = 1\n", encoding="utf-8")
    monkeypatch.setattr(collector, "_package_dir", lambda: package)
    monkeypatch.setattr(collector, "_checkout_root", lambda: root)
    return root, package


def set_mtime(path: Path, moment: datetime) -> None:
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


def collector_record() -> dict:
    return json.loads(paths.collector_path().read_text(encoding="utf-8"))


def open_anomalies() -> list[dict]:
    """Folded last-wins on (kind, subject), like every ledger reader."""
    folded: dict[tuple[str, str], dict] = {}
    for line in store.read_ledger(paths.anomalies_path()):
        if isinstance(line.get("kind"), str) and \
                isinstance(line.get("subject"), str):
            folded[(line["kind"], line["subject"])] = line
    return [line for line in folded.values()
            if line.get("resolved_at") is None]


def stale_lines() -> list[dict]:
    return [line for line in store.read_ledger(paths.anomalies_path())
            if line.get("kind") == "collector stale"
            and line.get("subject") == "collector"]


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=test@example.invalid",
         "-c", "user.name=foreman-test", *args],
        cwd=str(cwd), check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    return proc.stdout.strip()


def test_newer_source_file_opens_stale_on_next_tick(env, code_checkout):
    """A source file touched after the collector started opens one
    ``collector stale`` line on the very next tick, naming the restart verb,
    and the tick keeps the old record so the line stays open."""
    _, package = code_checkout
    source = package / "collector.py"
    set_mtime(source, NOW - timedelta(seconds=3600))

    tick(now=NOW)
    assert open_anomalies() == []
    assert collector_record()["code_mtime"] == (NOW - timedelta(seconds=3600)).timestamp()

    set_mtime(source, NOW)
    tick(now=later(60))

    lines = stale_lines()
    assert len(lines) == 1
    assert lines[0]["resolved_at"] is None
    assert lines[0]["detail"] == \
        f"collector stale since {iso(later(60))} \u2014 foreman collector restart"
    assert open_anomalies() == lines
    # The record is untouched: the next tick still sees the same gap.
    assert collector_record()["code_mtime"] == (NOW - timedelta(seconds=3600)).timestamp()
    tick(now=later(120))
    assert len(stale_lines()) == 1
    assert len(open_anomalies()) == 1


def test_moved_head_opens_stale(env, code_checkout):
    """A commit landing after the collector started moves the head the
    collector recorded, and the next tick flags it with the restart verb."""
    root, package = code_checkout
    git("init", "-q", "-b", "main", cwd=root)
    git("commit", "-q", "--allow-empty", "-m", "one", cwd=root)
    set_mtime(package / "collector.py", NOW - timedelta(seconds=3600))

    tick(now=NOW)
    assert open_anomalies() == []
    first_head = collector_record()["code_head"]
    assert isinstance(first_head, str) and len(first_head) == 40

    git("commit", "-q", "--allow-empty", "-m", "two", cwd=root)
    assert git("rev-parse", "HEAD", cwd=root) != first_head
    tick(now=later(60))

    lines = stale_lines()
    assert len(lines) == 1
    assert lines[0]["resolved_at"] is None
    assert lines[0]["detail"] == \
        f"collector stale since {iso(later(60))} \u2014 foreman collector restart"
    assert collector_record()["code_head"] == first_head


def test_fresh_collector_opens_nothing_and_restart_closes(env, code_checkout):
    """The first tick baselines instead of accusing, and a restarted
    collector — one that re-records the current checkout at startup — closes
    the line on its next tick with nothing closed by hand."""
    _, package = code_checkout
    set_mtime(package / "collector.py", NOW - timedelta(seconds=3600))

    tick(now=NOW)
    assert open_anomalies() == []

    set_mtime(package / "collector.py", NOW)
    tick(now=later(60))
    assert len(open_anomalies()) == 1

    record_startup_version()
    tick(now=later(120))

    assert open_anomalies() == []
    resolved = stale_lines()
    assert len(resolved) == 2
    assert resolved[0]["resolved_at"] is None
    assert resolved[1]["resolved_at"] == iso(later(120))


def test_no_git_records_no_head_but_still_catches_mtime(env, code_checkout):
    """A checkout that is not a git repository — an installed wheel has no
    head — records no head and never flags for it, yet a newer source file
    still opens the anomaly."""
    root, package = code_checkout
    assert collector._git_head(root) is None
    set_mtime(package / "collector.py", NOW - timedelta(seconds=3600))

    tick(now=NOW)
    assert collector_record()["code_head"] is None
    assert open_anomalies() == []

    set_mtime(package / "collector.py", NOW)
    tick(now=later(60))

    lines = stale_lines()
    assert len(lines) == 1
    assert "foreman collector restart" in lines[0]["detail"]
    assert collector_record()["code_head"] is None


def test_header_carries_staleness_and_pristine_fixture_unchanged(
        env, code_checkout, monkeypatch, capsys):
    """The status header names the stale collector beside its age, while a
    fixture with no collector record renders byte for byte as it does today."""
    from foreman.status import render

    _, package = code_checkout
    set_mtime(package / "collector.py", NOW - timedelta(seconds=3600))
    tick(now=NOW)
    assert "collector stale" not in render(now=NOW).splitlines()[0]

    set_mtime(package / "collector.py", NOW)
    tick(now=later(60))
    header = render(now=later(60)).splitlines()[0]
    assert "collector stale" in header
    assert "foreman collector restart" in header

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    from foreman import capacity
    monkeypatch.setattr(capacity, "_snapshot", lambda: {
        101: {"cmdline": "grok --prompt-file job.md"},
        102: {"cmdline": "muse exec --prompt-file job.md"},
    })
    assert cli.main(["status", "--fixture", FIXTURE]) == 0
    out = capsys.readouterr().out
    expected = (ROOT / "tests" / "v0" / "status_expected.txt").read_text(
        encoding="utf-8")
    assert out == expected


def test_restart_restarts_service_when_active(env, monkeypatch, capsys):
    """`foreman collector restart` restarts the user service where the
    collector runs as one, and reports that it did."""
    calls: list[str] = []

    def fake_restart() -> int:
        calls.append("restart")
        return 0

    monkeypatch.setattr(collector, "_service_active", lambda: True)
    monkeypatch.setattr(collector, "_service_restart", fake_restart)
    assert cli.main(["collector", "restart"]) == 0
    assert calls == ["restart"]
    assert "restarting" in capsys.readouterr().out


def test_restart_says_what_to_run_without_a_service(env, monkeypatch, capsys):
    """With no user service installed the verb fails nothing: it prints the
    commands that start the collector instead."""
    monkeypatch.setattr(collector, "_service_active", lambda: False)
    assert cli.main(["collector", "restart"]) == 0
    out = capsys.readouterr().out
    assert "systemctl --user start foreman-collector" in out
    assert "foreman collector run" in out


def test_restart_degrades_without_systemctl(env, monkeypatch):
    """A machine with no `systemctl` reads as 'no service', never an error."""
    def missing(*args, **kwargs):
        raise FileNotFoundError("no systemctl here")

    monkeypatch.setattr(subprocess, "run", missing)
    assert collector._service_active() is False


def test_restart_open_to_role_callers_but_not_strangers(
        env, monkeypatch, capsys):
    """The owner and any caller with a role may restart — whoever reads the
    stale screen — while an unknown session is refused by name."""
    monkeypatch.setattr(collector, "_service_active", lambda: False)
    assert cli.main(["collector", "restart"]) == 0
    capsys.readouterr()

    store.write_snapshot(paths.roster_path(), {"sessions": {
        "ses-sup00001": {"id": "ses-sup00001", "role": "supervisor",
                         "front": "comp", "state": "running"}}})
    monkeypatch.setenv(SESSION_ENV, "ses-sup00001")
    assert cli.main(["collector", "restart"]) == 0

    monkeypatch.setenv(SESSION_ENV, "ses-nobody00")
    assert cli.main(["collector", "restart"]) == 1
    assert "unregistered writer" in capsys.readouterr().err


def test_startup_record_carries_started_at_and_tick_leaves_it(
        env, code_checkout):
    """The collector's startup record names when it started, and a tick
    leaves that stamp as it was."""
    record_startup_version()
    first = collector_record()
    assert isinstance(first.get("started_at"), str)
    datetime.fromisoformat(first["started_at"])

    tick(now=NOW)
    second = collector_record()
    assert second["started_at"] == first["started_at"]

    tick(now=later(60))
    third = collector_record()
    assert third["started_at"] == first["started_at"]
