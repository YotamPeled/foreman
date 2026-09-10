"""panel.json in the v5 shape: queue, capacity, milestones, tree, landings.

The v5 golden fixture is the same world ``foreman status --fixture``
prints; these tests fold it through ``panel_feed.gather`` and check the
structured blocks against that screen. A field that drifts from status
is a fold that re-derived instead of reusing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman import cli, panel_feed, status
from foreman.caller import SESSION_ENV
from foreman.status import NOW_ENV

ROOT = Path(__file__).resolve().parents[2]
V5_FIXTURE = ROOT / "tests" / "v0" / "fixture-v5"
PINNED_NOW = "2026-09-10T12:00:00+00:00"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(V5_FIXTURE))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    return tmp_path


def feed_of(env) -> dict:
    return panel_feed.gather()


def by_name(rows: list[dict], name: str) -> dict:
    for row in rows:
        if row.get("name") == name or row.get("front") == name:
            return row
    raise AssertionError(f"{name!r} missing from {rows!r}")


def test_v5_fixture_carries_the_five_blocks(env):
    """front_queue, capacity, and per-front milestones/tree/landings."""
    feed = feed_of(env)
    queue = feed["front_queue"]
    assert queue["count"] == 2
    harbor = by_name(queue["rows"], "harbor")
    orbit = by_name(queue["rows"], "orbit")
    assert harbor["front"] == "harbor"
    assert harbor["state"] == "queued"
    assert harbor["reason"] == "pool grok: cap 1, reserved 1, wants 1"
    assert harbor["reserved"] == {}
    assert orbit["front"] == "orbit"
    assert orbit["state"] == "active"
    assert orbit["reason"] == "running"
    assert orbit["reserved"] == {"grok": 1}

    capacity = feed["capacity"]
    assert capacity["count"] == 1
    grok = capacity["rows"][0]
    assert grok["pool"] == "grok"
    assert grok["held"] == 0
    assert grok["reserved"] == 1
    assert grok["cap"] == 1

    fronts = {row["name"]: row for row in feed["fronts"]}
    harbor_front = fronts["harbor"]
    assert harbor_front["milestones"] == []
    assert harbor_front["tree"] == []
    assert harbor_front["landings"] == []

    orbit_front = fronts["orbit"]
    assert orbit_front["milestones"] == [{
        "id": "mil-1",
        "title": "The screen",
        "landed": 0,
        "leaves": 1,
        "split_from": 1,
    }]
    tree = orbit_front["tree"]
    assert [(row["id"], row["depth"], row["kind"], row["title"],
             row["state"], row["waits"]) for row in tree] == [
        ("mil-1", 1, "milestone", "The screen", "", ""),
        ("tsk-1", 2, "task", "status shape", "queued", ""),
        ("job-1", 3, "job", "implement it", "queued", "ready"),
        ("job-land1", 2, "job", "land implement it", "queued", "ready"),
    ]
    landings = orbit_front["landings"]
    assert landings[0]["id"] == "job-land1"
    assert landings[0]["kind"] == "job"
    assert landings[0]["state"] == "queued"
    assert landings[0]["sha"] == ""
    assert landings[0]["reason"] == ""
    behind = landings[1]
    assert behind["kind"] == "behind"
    assert behind["behind"] == "c0ffee1deadbeefc0ffee1deadbeefc0ffee12"
    assert behind["sha"] == behind["behind"]
    assert behind["reason"] == "behind main"


def test_rows_agree_with_status(env, monkeypatch, capsys):
    """Same landed counts and wait reasons as the status screen."""
    feed = feed_of(env)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out

    harbor = by_name(feed["front_queue"]["rows"], "harbor")
    orbit = by_name(feed["front_queue"]["rows"], "orbit")
    assert f"  harbor  {harbor['reason']}" in out
    assert f"  orbit  {orbit['reason']}" in out

    mil = by_name(feed["fronts"], "orbit")["milestones"][0]
    assert (f"M1 {mil['title']}: {mil['landed']}/{mil['leaves']} pieces "
            f"(split from {mil['split_from']})") in out
    grok = feed["capacity"]["rows"][0]
    assert (f"{grok['pool']}: held {grok['held']} / reserved "
            f"{grok['reserved']} / cap {grok['cap']}") in out
    assert "      job-land1  queued" in out
    assert "      behind main (c0ffee1)" in out

    screen = status.render(now=datetime(2026, 9, 10, 12, 0, 0,
                                        tzinfo=timezone.utc))
    assert harbor["reason"] in screen
    assert orbit["reason"] in screen


def test_empty_v5_blocks_are_lists(tmp_path, monkeypatch):
    """No fronts: the five blocks exist and carry no rows."""
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    feed = panel_feed.gather()
    assert feed["front_queue"] == {"count": 0, "rows": []}
    assert feed["capacity"]["rows"] == []
    assert feed["fronts"] == []
