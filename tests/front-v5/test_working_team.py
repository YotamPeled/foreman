"""The working team is derived from the map and tree, not the owner's line.

The world is a fresh FOREMAN_STATE. Front records are written directly so
the tests name the ceiling they need; ``front add`` is not the seam.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foreman import cli, entities, fronts, ids, paths, store
from foreman.caller import SESSION_ENV
from foreman.team import TeamEntry, apply_team_signals, derive_team

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "foreman.toml").write_text(
        "[pool.grok]\ncap = 6\nroles = [\"grok\"]\n"
        "[pool.muse]\ncap = 6\nroles = [\"muse\"]\n"
        "[pool.claude]\ncap = 4\nroles = [\"opus\"]\n"
        "[pool.codex]\ncap = 4\nroles = [\"astra\"]\n",
        encoding="utf-8")
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


def open_for(front: str) -> list[dict]:
    return [rec for rec in fronts.open_reservations()
            if rec.get("front") == front]


def reserved_count(front: str, role: str) -> int:
    return fronts.reserved_by_front_role().get((front, role), 0)


def test_reservation_holds_the_derived_count_not_the_ceiling(env, capsys):
    """Ceiling 3, four leaves derive 2: the reservation is 2, not 3."""
    write_front("alpha", grok=3)
    write_leaves("alpha", 4)
    assert cli.main(["front", "team", "alpha"]) == 0
    capsys.readouterr()
    assert cli.main(["front", "reserve", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "reserved grok 2 (builders)" in out
    assert reserved_count("alpha", "grok") == 2
    recs = [r for r in open_for("alpha")
            if r.get("pool") == "grok" and r.get("role") == "grok"]
    assert len(recs) == 1
    assert recs[0]["count"] == 2


def test_no_tree_reserves_supervisor_and_one_builder(env, capsys):
    write_front("alpha", grok=3)
    assert cli.main(["front", "team", "alpha"]) == 0
    capsys.readouterr()
    assert cli.main(["front", "reserve", "alpha"]) == 0
    capsys.readouterr()
    assert reserved_count("alpha", "grok") == 1
    assert reserved_count("alpha", "supervisor") == 1


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


def seed_working(name: str, grok: int, grok_ceiling: int,
                 muse: int, muse_ceiling: int) -> dict:
    record = fronts.read_front_record(name)
    assert record is not None
    team = [
        {"role": "supervisor", "pool": "grok", "count": 1, "ceiling": 1,
         "reason": "supervisor", "agent": "grok-4.6", "model": "grok-4.6",
         "effort": "high"},
        {"role": "builder", "pool": "grok", "count": grok,
         "ceiling": grok_ceiling, "reason": "seeded",
         "agent": "grok-4.6", "model": "grok-4.6", "effort": "high"},
        {"role": "builder", "pool": "muse", "count": muse,
         "ceiling": muse_ceiling, "reason": "seeded",
         "agent": "muse", "model": "muse", "effort": "high"},
    ]
    return fronts.revise_front(
        name, record, "owner",
        working_team=team, derived_at=iso(NOW))


def evidence_claims(front: str) -> list[str]:
    try:
        lines = store.read_ledger(paths.front_evidence_path(front))
    except OSError:
        return []
    return [str(line.get("claim") or "") for line in lines
            if isinstance(line, dict)]


def test_pool_out_raises_the_other_builder_and_writes_evidence(env):
    """Grok is out: muse (the other builder) steps 1 -> 2 within 3."""
    write_front("alpha", grok=3, muse=3)
    seed_working("alpha", grok=2, grok_ceiling=3, muse=1, muse_ceiling=3)
    store.append_ledger(paths.pools_path(), {
        "id": "grok", "pool": "grok",
        "out_until": iso(NOW + timedelta(hours=1)),
        "because": "quota",
    })
    record = fronts.read_front_record("alpha")
    apply_team_signals("alpha", record, now=NOW, who="collector")
    updated = fronts.read_front_record("alpha")
    assert updated is not None
    muse = [e for e in updated["working_team"]
            if e.get("role") == "builder" and e.get("pool") == "muse"][0]
    grok = [e for e in updated["working_team"]
            if e.get("role") == "builder" and e.get("pool") == "grok"][0]
    assert muse["count"] == 2
    assert grok["count"] == 2
    claims = evidence_claims("alpha")
    assert any(
        "team adjusted: builder muse 1 -> 2 (pool out)" in claim
        for claim in claims), claims


def test_signal_at_the_ceiling_files_an_ask_and_changes_nothing(env):
    write_front("alpha", grok=3, muse=3)
    seed_working("alpha", grok=3, grok_ceiling=3, muse=3, muse_ceiling=3)
    store.append_ledger(paths.pools_path(), {
        "id": "grok", "pool": "grok",
        "out_until": iso(NOW + timedelta(hours=1)),
        "because": "quota",
    })
    before = fronts.read_front_record("alpha")
    assert before is not None
    apply_team_signals("alpha", before, now=NOW, who="collector")
    after = fronts.read_front_record("alpha")
    assert after is not None
    assert after.get("working_team") == before.get("working_team")
    inbox = store.read_ledger(paths.inbox_path())
    assert inbox, "expected an owner ask"
    question = str(inbox[0].get("question") or "")
    assert "front alpha" in question
    assert "(pool out)" in question
    assert inbox[0].get("kind") == "money"
    assert evidence_claims("alpha") == []
