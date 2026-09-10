"""A front reserves its team against the pool cap until it is released.

The world caps a fake pool at 3. Front records are written directly so
the tests name the team they need; ``front add`` is not the seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foreman import cli, entities, fronts, ids, paths, store
from foreman.caller import SESSION_ENV
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter


class FakeAdapter(PoolAdapter):
    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    def launch(self, ctx: LaunchContext) -> int:
        raise AssertionError("reservations tests do not spawn")

    def observe(self, session: Session) -> dict:
        return {}

    def command_str(self, ctx: LaunchContext) -> str:
        return "fake-exec"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from foreman import pools

    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    pools.register("fake", FakeAdapter())
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        "[pool.fake]\ncap = 3\nroles = [\"muse\"]\n", encoding="utf-8")
    try:
        yield tmp_path
    finally:
        pools.unregister("fake")


def write_v5(name: str, builders: int, pool: str = "fake",
             reviewers: int = 0) -> None:
    paths.front_dir(name).mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "fake", "pool": pool, "model": "fake-test-model",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "fake", "pool": pool, "model": "fake-test-model",
         "effort": "high", "count": builders, "role": "builder"},
    ]
    if reviewers:
        team.append(
            {"agent": "fake", "pool": pool, "model": "fake-test-model",
             "effort": "low", "count": reviewers, "role": "reviewer"})
    line = entities.Front(
        id=ids.mint("front"), name=name, state="active", shape="v5",
        goal="Hold the team.", finish_line="The front keeps its slots.",
        allocation={"muse": builders + reviewers}, team=team,
    ).to_dict()
    store.append_ledger(paths.front_record_path(name), line)


def write_old(name: str, **allocation: int) -> None:
    paths.front_dir(name).mkdir(parents=True, exist_ok=True)
    line = entities.Front(
        id=ids.mint("front"), name=name, state="active",
        want="Old shape.", done_when="It finished.", land_on="main",
        allocation=allocation,
    ).to_dict()
    store.append_ledger(paths.front_record_path(name), line)


def open_for(front: str) -> list[dict]:
    return [rec for rec in fronts.open_reservations()
            if rec.get("front") == front]


def test_a_v5_front_reserves_builders_and_not_the_supervisor(env, capsys):
    """Two builders on the fake pool become one open reservation; the
    supervisor entry holds nothing."""
    write_v5("A", builders=2)
    assert cli.main(["front", "reserve", "A", "--phase", "builders"]) == 0
    out = capsys.readouterr().out
    assert "reserved fake 2 (builders)" in out
    recs = open_for("A")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["id"].startswith("rsv-")
    assert rec["front"] == "A"
    assert rec["pool"] == "fake"
    assert rec["role"] == "muse"
    assert rec["count"] == 2
    assert rec["phase"] == "builders"
    assert rec.get("released_at") is None
    assert rec.get("at")
    assert rec.get("by") == "owner"
    assert Path(paths.reservations_path()).is_file()


def test_a_second_reserve_of_the_same_phase_is_idempotent(env, capsys):
    write_v5("A", builders=2)
    assert cli.main(["front", "reserve", "A"]) == 0
    capsys.readouterr()
    assert cli.main(["front", "reserve", "A"]) == 0
    assert "already held" in capsys.readouterr().out
    assert len(store.read_ledger(paths.reservations_path())) == 1
    assert len(open_for("A")) == 1


def test_front_b_is_refused_when_a_holds_two_of_a_cap_of_three(env, capsys):
    """B wanting two is named with the cap, who holds what, and what it
    wants. B wanting one fits in the leftover slot."""
    write_v5("A", builders=2)
    write_v5("B", builders=2)
    assert cli.main(["front", "reserve", "A"]) == 0
    capsys.readouterr()
    assert cli.main(["front", "reserve", "B"]) == 1
    err = capsys.readouterr().err
    assert "pool fake: cap 3, reserved 2 by A, wants 2" in err
    assert open_for("B") == []

    write_v5("B-one", builders=1)
    assert cli.main(["front", "reserve", "B-one"]) == 0
    recs = open_for("B-one")
    assert len(recs) == 1 and recs[0]["count"] == 1


def test_release_frees_the_slots_so_b_can_take_two(env, capsys):
    write_v5("A", builders=2)
    write_v5("B", builders=2)
    assert cli.main(["front", "reserve", "A"]) == 0
    assert cli.main(["front", "reserve", "B"]) == 1
    capsys.readouterr()
    assert cli.main(["front", "release", "A"]) == 0
    out = capsys.readouterr().out
    assert "A: released 1" in out
    assert open_for("A") == []
    folded = store.fold_by_id(store.read_ledger(paths.reservations_path()))
    assert folded[0]["released_at"] is not None
    assert cli.main(["front", "reserve", "B"]) == 0
    recs = open_for("B")
    assert len(recs) == 1 and recs[0]["count"] == 2


def test_front_done_releases_too(env, capsys):
    write_v5("A", builders=2)
    write_v5("B", builders=2)
    assert cli.main(["front", "reserve", "A"]) == 0
    capsys.readouterr()
    assert cli.main(["front", "done", "A"]) == 0
    out = capsys.readouterr().out
    assert "A done" in out
    assert open_for("A") == []
    assert cli.main(["front", "reserve", "B"]) == 0
    assert open_for("B")[0]["count"] == 2


def test_front_close_releases_too(env, capsys):
    write_v5("A", builders=2)
    assert cli.main(["front", "reserve", "A"]) == 0
    capsys.readouterr()
    assert cli.main(["front", "close", "A"]) == 0
    assert open_for("A") == []


def test_an_old_shape_front_reserves_its_allocation_as_builders(env, capsys):
    write_old("C", muse=2)
    assert cli.main(["front", "reserve", "C"]) == 0
    recs = open_for("C")
    assert len(recs) == 1
    assert recs[0]["phase"] == "builders"
    assert recs[0]["pool"] == "fake"
    assert recs[0]["role"] == "muse"
    assert recs[0]["count"] == 2
