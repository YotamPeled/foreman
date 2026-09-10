"""The working team is derived from the map and tree, not the owner's line.

The world is a fresh FOREMAN_STATE. Front records are written directly so
the tests name the ceiling they need; ``front add`` is not the seam.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foreman import cli, entities, fronts, ids, paths, store
from foreman.caller import SESSION_ENV
from foreman.team import TeamEntry, derive_team

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    return tmp_path


def iso(moment: datetime) -> str:
    return moment.isoformat()


def write_front(name: str, grok: int = 3, muse: int = 3,
                opus: int = 1, astra: int = 1) -> dict:
    paths.front_dir(name).mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "grok-4.6", "pool": "grok", "model": "grok-4.6",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "grok-4.6", "pool": "grok", "model": "grok-4.6",
         "effort": "high", "count": grok, "role": "builder"},
        {"agent": "muse", "pool": "muse", "model": "muse",
         "effort": "high", "count": muse, "role": "builder"},
        {"agent": "opus-5", "pool": "claude", "model": "claude-opus-5",
         "effort": "high", "count": opus, "role": "backup-builder"},
        {"agent": "astra-6", "pool": "codex", "model": "gpt-6-astra",
         "effort": "low", "count": astra, "role": "reviewer"},
    ]
    line = entities.Front(
        id=ids.mint("front"), name=name, state="queued", shape="v5",
        goal="Derive the working team.",
        finish_line="The working team is derived.",
        allocation={"grok": grok, "muse": muse, "opus": opus, "astra": astra},
        team=team,
        supervisor={"agent": "grok-4.6", "pool": "grok",
                    "model": "grok-4.6", "effort": "high"},
    ).to_dict()
    store.append_ledger(paths.front_record_path(name), line)
    record = fronts.read_front_record(name)
    assert record is not None
    return record


def write_leaves(front: str, n: int, *, mechanical: int = 0,
                 parent: str = "mil-1") -> None:
    store.append_ledger(paths.front_tree_path(front), {
        "id": parent, "front": front, "parent": front,
        "kind": "milestone", "title": "next", "state": "",
    })
    for i in range(n):
        store.append_ledger(paths.front_tree_path(front), {
            "id": f"job-{i + 1:02d}", "front": front, "parent": parent,
            "kind": "job", "title": f"leaf {i + 1}",
            "mechanical": i < mechanical,
        })


def grok_builder(entries: list[TeamEntry]) -> TeamEntry:
    found = [e for e in entries
             if e.role == "builder" and e.pool == "grok"]
    assert found, entries
    return found[0]


def muse_builder(entries: list[TeamEntry]) -> TeamEntry:
    found = [e for e in entries
             if e.role == "builder" and e.pool == "muse"]
    assert found, entries
    return found[0]


def test_four_leaves_under_a_ceiling_of_three_derives_fewer_with_reason(env):
    """A ceiling of 3 and 4 leaves is not 3: verification rate is 2."""
    record = write_front("alpha", grok=3)
    write_leaves("alpha", 4)
    tree = store.fold_by_id(store.read_ledger(paths.front_tree_path("alpha")))
    entries = derive_team(record, tree, [])
    grok = grok_builder(entries)
    assert grok.count < 3
    assert grok.count == 2
    assert grok.ceiling == 3
    assert "4 leaves in the next milestone" in grok.reason


def test_twenty_leaves_derives_the_ceiling(env):
    record = write_front("alpha", grok=3)
    write_leaves("alpha", 20)
    tree = store.fold_by_id(store.read_ledger(paths.front_tree_path("alpha")))
    grok = grok_builder(derive_team(record, tree, []))
    assert grok.count == 3
    assert grok.ceiling == 3
    assert "20 leaves in the next milestone" in grok.reason


def test_mechanical_share_under_threshold_derives_zero_muse(env):
    record = write_front("alpha", grok=3, muse=3)
    write_leaves("alpha", 4, mechanical=1)
    tree = store.fold_by_id(store.read_ledger(paths.front_tree_path("alpha")))
    muse = muse_builder(derive_team(record, tree, []))
    assert muse.count == 0
    assert muse.ceiling == 3
    assert "mechanical share under threshold" in muse.reason


def test_derive_team_never_exceeds_the_owner_ceiling(env):
    record = write_front("alpha", grok=2)
    write_leaves("alpha", 20)
    tree = store.fold_by_id(store.read_ledger(paths.front_tree_path("alpha")))
    for entry in derive_team(record, tree, []):
        assert entry.count <= entry.ceiling, entry


def test_front_team_prints_count_of_ceiling_and_reason(env, capsys):
    write_front("alpha", grok=3)
    write_leaves("alpha", 4)
    assert cli.main(["front", "team", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "builder grok: 2 of 3 (4 leaves in the next milestone)" in out
    record = fronts.read_front_record("alpha")
    assert record is not None
    assert record.get("derived_at")
    working = record.get("working_team")
    assert isinstance(working, list) and working
    grok = [e for e in working
            if e.get("role") == "builder" and e.get("pool") == "grok"][0]
    assert grok["count"] == 2
    assert grok["ceiling"] == 3


def test_queued_front_records_working_team_with_no_tree(env):
    """No leaves yet: supervisor and one builder, stamped derived_at."""
    record = write_front("alpha", grok=3)
    entries = derive_team(record, [], [])
    grok = grok_builder(entries)
    assert grok.count == 1
    assert grok.ceiling == 3
    assert grok.reason == "no tree yet"
    supervisor = [e for e in entries if e.role == "supervisor"][0]
    assert supervisor.count == 1


def test_milestone_land_records_working_team_again(env, capsys):
    write_front("alpha", grok=3)
    write_leaves("alpha", 4)
    assert cli.main(["front", "team", "alpha"]) == 0
    capsys.readouterr()
    first = fronts.read_front_record("alpha")
    assert first is not None
    assert first.get("derived_at")
    assert cli.main([
        "node", "revise", "alpha", "mil-1",
        "--state", "landed", "--reason", "milestone done",
    ]) == 0
    capsys.readouterr()
    second = fronts.read_front_record("alpha")
    assert second is not None
    assert second.get("derived_at")
    working = second.get("working_team")
    assert isinstance(working, list) and working
