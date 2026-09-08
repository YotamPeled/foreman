"""Monitors: measured by supervisors, shown on the front's card.

Every test drives the real entry points against a fresh FOREMAN_STATE
and FOREMAN_CONFIG directory. Collector tests run a real tick with the
clock injected; status tests drive the real CLI with the clock pinned.
No test writes to a real state directory.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, monitors, paths, store
from foreman.caller import SESSION_ENV
from foreman.collector import tick

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
OTHER_SUP = "ses-sup0002"

BRIEF = """name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 2

[[task]]
title = "first"
scope = \"\"\"
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
\"\"\"
verify = "make check first"
size = 3
after = []

[[monitor]]
question = "how full is the queue?"
measure = "count-queue"
unit = "jobs"
of = 10
every = "10m"
alert = "> 0.80"

[[monitor]]
question = "how slow are tests?"
measure = "test-seconds"
unit = "s"
every = "landing"
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


def iso(moment: datetime) -> str:
    return moment.isoformat()


def write_brief(root: Path, name: str, text: str | None = None) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        BRIEF.format(name=name) if text is None else text, encoding="utf-8")
    return brief_dir


def add_front_at(env: Path, monkeypatch, capsys, name: str = "mon") -> None:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["front", "add", str(write_brief(env, name))]) == 0
    capsys.readouterr()


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def sup_session(sid, front):
    return entities.Session.from_dict({
        "id": sid, "role": "supervisor", "pool": "opus", "model": "opus",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": iso(NOW - timedelta(minutes=30)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }).to_dict()


def seed_supervisor(front, sid=SUP):
    store.write_snapshot(paths.roster_path(),
                         {"sessions": {sid: sup_session(sid, front)}})


def open_anomalies():
    try:
        records = store.read_ledger(paths.anomalies_path())
    except OSError:
        return []
    order: list[tuple[str, str]] = []
    folded: dict[tuple[str, str], dict] = {}
    for record in records:
        key = (record.get("kind"), record.get("subject"))
        if key not in folded:
            order.append(key)
        folded[key] = record
    return [folded[key] for key in order
            if folded[key].get("resolved_at") is None]


def test_measure_unknown_monitor_is_refused(env, monkeypatch, capsys):
    """A measurement for a monitor the brief does not declare is refused
    by name, and nothing is appended."""
    add_front_at(env, monkeypatch, capsys)
    seed_supervisor("mon")
    before = store.read_ledger(paths.front_measurements_path("mon"))
    assert run(monkeypatch, ["measure", "mon", "no-such-monitor",
                             "--value", "3",
                             "--command", "count-queue",
                             "--output", "3"], SUP) == 1
    _, err = capsys.readouterr()
    assert "no-such-monitor" in err
    assert store.read_ledger(paths.front_measurements_path("mon")) == before


def test_measure_without_command_is_refused(env, monkeypatch, capsys):
    """A measurement with no command behind it is not a measurement:
    refused, and nothing is appended."""
    add_front_at(env, monkeypatch, capsys)
    seed_supervisor("mon")
    assert run(monkeypatch, ["measure", "mon", "count-queue",
                             "--value", "3",
                             "--output", "3"], SUP) == 1
    _, err = capsys.readouterr()
    assert "'--command'" in err
    assert store.read_ledger(paths.front_measurements_path("mon")) == []


def test_measure_ok_appends_measurement(env, monkeypatch, capsys):
    """The supervisor's measurement lands on measurements.jsonl with its
    value, denominator, command and output."""
    add_front_at(env, monkeypatch, capsys)
    seed_supervisor("mon")
    assert run(monkeypatch, ["measure", "mon", "count-queue",
                             "--value", "8", "--of", "10",
                             "--command", "count-queue",
                             "--output", "8"], SUP) == 0
    capsys.readouterr()
    lines = store.read_ledger(paths.front_measurements_path("mon"))
    assert len(lines) == 1
    assert lines[0]["monitor"] == "count-queue"
    assert lines[0]["value"] == 8.0
    assert lines[0]["of"] == 10.0
    assert lines[0]["command"] == "count-queue"
    assert lines[0]["output_ref"] == "8"
    assert lines[0]["by"] == SUP


def test_measure_by_question_and_wrong_supervisor(env, monkeypatch, capsys):
    """The monitor may be named by its question; a supervisor from
    another front is refused, naming both fronts."""
    add_front_at(env, monkeypatch, capsys)
    seed_supervisor("mon")
    store.write_snapshot(paths.roster_path(), {"sessions": {
        OTHER_SUP: sup_session(OTHER_SUP, "elsewhere")}})
    assert run(monkeypatch, ["measure", "mon", "count-queue",
                             "--value", "1",
                             "--command", "count-queue",
                             "--output", "1"], OTHER_SUP) == 1
    _, err = capsys.readouterr()
    assert "elsewhere" in err
    assert "mon" in err
    assert store.read_ledger(paths.front_measurements_path("mon")) == []
    # By question works for the right supervisor.
    store.write_snapshot(paths.roster_path(), {"sessions": {
        SUP: sup_session(SUP, "mon")}})
    assert run(monkeypatch, ["measure", "mon", "how full is the queue?",
                             "--value", "1",
                             "--command", "count-queue",
                             "--output", "1"], SUP) == 0
    capsys.readouterr()
    assert store.read_ledger(
        paths.front_measurements_path("mon"))[0]["monitor"] == "count-queue"


def test_stale_monitor_raised_by_real_tick(env, monkeypatch, capsys):
    """A real collector tick flags a monitor whose newest measurement is
    older than twice its `every` cadence."""
    add_front_at(env, monkeypatch, capsys)
    seed_supervisor("mon")
    store.append_ledger(paths.front_measurements_path("mon"), {
        "monitor": "count-queue", "value": 4.0, "of": 10.0, "status": "",
        "command": "count-queue", "output_ref": "4",
        "at": iso(NOW - timedelta(minutes=30)),
    }, session_id=SUP)
    payload = tick(now=NOW)
    kinds = {(line["kind"], line["subject"]) for line in open_anomalies()}
    assert ("monitor stale", "mon:count-queue") in kinds
    assert "mon" in payload["monitors"]
    assert payload["monitors"]["mon"]["count-queue"]["stale"] is True
    # A fresh measurement on the event-cadence monitor is never stale.
    assert payload["monitors"].get("test-seconds") is None


def test_stale_resolves_on_fresh_measurement(env, monkeypatch, capsys):
    """The next tick with a fresh measurement resolves the stale line."""
    add_front_at(env, monkeypatch, capsys)
    seed_supervisor("mon")
    store.append_ledger(paths.front_measurements_path("mon"), {
        "monitor": "count-queue", "value": 4.0, "of": 10.0, "status": "",
        "command": "count-queue", "output_ref": "4",
        "at": iso(NOW - timedelta(minutes=30)),
    }, session_id=SUP)
    tick(now=NOW)
    assert any(line["kind"] == "monitor stale"
               for line in open_anomalies())
    store.append_ledger(paths.front_measurements_path("mon"), {
        "monitor": "count-queue", "value": 5.0, "of": 10.0, "status": "",
        "command": "count-queue", "output_ref": "5",
        "at": iso(NOW),
    }, session_id=SUP)
    tick(now=NOW + timedelta(seconds=1))
    assert not [line for line in open_anomalies()
                if line["kind"] == "monitor stale"]


def test_alert_raised_by_real_tick(env, monkeypatch, capsys):
    """A real collector tick flags a monitor whose alert expression holds
    of the latest value (the value/of ratio where a denominator is
    known). Here 9/10 = 0.9 trips `> 0.80`."""
    add_front_at(env, monkeypatch, capsys)
    seed_supervisor("mon")
    store.append_ledger(paths.front_measurements_path("mon"), {
        "monitor": "count-queue", "value": 9.0, "of": 10.0, "status": "",
        "command": "count-queue", "output_ref": "9",
        "at": iso(NOW),
    }, session_id=SUP)
    payload = tick(now=NOW)
    kinds = {(line["kind"], line["subject"]) for line in open_anomalies()}
    assert ("monitor alert", "mon:count-queue") in kinds
    assert payload["monitors"]["mon"]["count-queue"]["alerting"] is True
    detail = [line for line in open_anomalies()
              if line["kind"] == "monitor alert"][0]["detail"]
    assert "how full is the queue?" in detail


def test_alert_quiet_below_threshold(env, monkeypatch, capsys):
    """Same monitor, calm value: the tick snapshots it with no anomaly."""
    add_front_at(env, monkeypatch, capsys)
    seed_supervisor("mon")
    store.append_ledger(paths.front_measurements_path("mon"), {
        "monitor": "count-queue", "value": 2.0, "of": 10.0, "status": "",
        "command": "count-queue", "output_ref": "2",
        "at": iso(NOW),
    }, session_id=SUP)
    payload = tick(now=NOW)
    assert open_anomalies() == []
    assert payload["monitors"]["mon"]["count-queue"]["alerting"] is False


def test_alert_never_evals(tmp_path):
    """The alert parser evaluates a comparison, never the string: a
    hostile alert does not parse and never fires."""
    assert monitors.parse_alert("__import__('os').system('id')") is None
    assert monitors.evaluate_alert(1.0, "__import__('os').system('id')") is False
    assert monitors.parse_alert("< 0.80") == ("<", 0.80)
    assert monitors.evaluate_alert(0.5, "< 0.80") is True
    assert monitors.evaluate_alert(0.9, "< 0.80") is False


def test_status_lines_for_front_with_monitors(env, monkeypatch, capsys):
    """Declared monitors render as `question — value/of · age ago ·
    trend`: one measurement shows no trend, two show the arrow."""
    add_front_at(env, monkeypatch, capsys)
    seed_supervisor("mon")
    assert run(monkeypatch, ["measure", "mon", "count-queue",
                             "--value", "4", "--of", "10",
                             "--command", "count-queue",
                             "--output", "4"], SUP) == 0
    capsys.readouterr()
    # Backdate the first measurement so the age is stable under the
    # pinned clock below.
    ledger_path = paths.front_measurements_path("mon")
    first = store.read_ledger(ledger_path)[0]
    ledger_path.write_text("", encoding="utf-8")
    store.append_ledger(ledger_path, dict(first, at=iso(
        NOW - timedelta(minutes=5))), session_id=SUP)
    monkeypatch.setenv("FOREMAN_NOW", iso(NOW))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "how full is the queue? \u2014 4/10 jobs \u00b7 5m ago" in out
    single = [line for line in out.splitlines()
              if "how full is the queue?" in line][0]
    assert "\u2191" not in single and "\u2193" not in single
    # A second, higher measurement adds the upward trend.
    seed_supervisor("mon")
    assert run(monkeypatch, ["measure", "mon", "count-queue",
                             "--value", "6", "--of", "10",
                             "--command", "count-queue",
                             "--output", "6"], SUP) == 0
    capsys.readouterr()
    ledger = store.read_ledger(ledger_path)
    ledger_path.write_text("", encoding="utf-8")
    for entry in ledger[:-1]:
        store.append_ledger(ledger_path, entry,
                            session_id=entry.get("by"))
    store.append_ledger(ledger_path, dict(ledger[-1], at=iso(NOW)),
                        session_id=SUP)
    monkeypatch.setenv("FOREMAN_NOW", iso(NOW))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "how full is the queue? \u2014 6/10 jobs \u00b7 0s ago \u00b7 \u2191" \
        in out


def test_status_lines_for_front_with_no_monitors(env, monkeypatch, capsys):
    """A front with no declared monitors still shows the two free ones:
    doing now and progress, in the same shape."""
    text = BRIEF.format(name="plain")
    task_only = text.split("[[monitor]]")[0]
    write_brief(env, "plain", task_only)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["front", "add", str(env / "plain")]) == 0
    capsys.readouterr()
    store.write_snapshot(paths.roster_path(), {"sessions": {
        SUP: sup_session(SUP, "plain")}})
    store.write_snapshot(paths.checkpoint_path(SUP),
                         entities.Checkpoint(
                             session=SUP, doing="Splitting the work",
                             next="Dispatch jobs").to_dict())
    monkeypatch.setenv("FOREMAN_NOW", iso(NOW))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "doing now \u2014 Splitting the work \u00b7" in out
    assert "progress \u2014 0/1 tasks" in out
    assert "count-queue" not in out
