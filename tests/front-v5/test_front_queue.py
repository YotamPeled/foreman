"""The front queue: order, why each waits, start, stop, resume.

The world caps a fake pool at 3. Front records are written directly so
the tests name the team they need; ``front add`` is not the seam. Worker
and supervisor spawn is substituted: nothing here reaches systemd.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foreman import cli, entities, fronts, ids, paths, store
from foreman.caller import SESSION_ENV
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


class FakeAdapter(PoolAdapter):
    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    def launch(self, ctx: LaunchContext) -> int:
        raise AssertionError("front-queue tests do not spawn")

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
             prefer: int = 0, state: str = "queued",
             model: str = "fake-test-model", effort: str = "high") -> dict:
    paths.front_dir(name).mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "fake", "pool": pool, "model": model,
         "effort": effort, "count": 1, "role": "supervisor"},
        {"agent": "fake", "pool": pool, "model": model,
         "effort": effort, "count": builders, "role": "builder"},
    ]
    line = entities.Front(
        id=ids.mint("front"), name=name, state=state, shape="v5",
        prefer=prefer,
        goal="Run in queue order.", finish_line="The top front starts.",
        allocation={"muse": builders}, team=team,
        supervisor={"agent": "fake", "pool": pool, "model": model,
                    "effort": effort},
    ).to_dict()
    store.append_ledger(paths.front_record_path(name), line)
    record = fronts.read_front_record(name)
    assert record is not None
    return record


def test_front_queue_prints_team_fits_then_behind(env, capsys):
    """Two queued v5 fronts wanting 2 and 2 on a cap of 3: the top
    prints team fits and the second prints behind it."""
    write_v5("alpha", builders=2)
    write_v5("beta", builders=2)
    assert cli.main(["front", "queue"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "alpha  team fits",
        "beta  behind alpha",
    ]


def test_front_queue_orders_by_prefer_then_requested_at(env, capsys):
    """Lower prefer is first; equal prefer keeps requested_at order."""
    write_v5("late", builders=1, prefer=1)
    write_v5("early", builders=1, prefer=0)
    assert cli.main(["front", "queue"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "early  team fits",
        "late  behind early",
    ]


def test_front_queue_names_the_pool_when_the_top_does_not_fit(env, capsys):
    """The top front whose builders do not fit names cap, reserved and
    wants; the second is still behind the top."""
    write_v5("held", builders=2, state="active")
    assert cli.main(["front", "reserve", "held"]) == 0
    capsys.readouterr()
    write_v5("alpha", builders=2)
    write_v5("beta", builders=2)
    assert cli.main(["front", "queue"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "alpha  pool fake: cap 3, reserved 2, wants 2",
        "beta  behind alpha",
    ]


def test_team_fits_is_none_when_every_pool_has_room(env):
    write_v5("alpha", builders=2)
    assert fronts.team_fits(fronts.read_front_record("alpha")) is None


def test_team_fits_names_the_pool_numbers(env, capsys):
    write_v5("held", builders=2, state="active")
    assert cli.main(["front", "reserve", "held"]) == 0
    capsys.readouterr()
    write_v5("alpha", builders=2)
    assert fronts.team_fits(fronts.read_front_record("alpha")) == (
        "pool fake: cap 3, reserved 2, wants 2")


def test_front_queue_refuses_a_supervisor(env, monkeypatch, capsys):
    write_v5("alpha", builders=1)
    store.write_snapshot(paths.roster_path(), {"sessions": {
        "ses-sup0001": Session(
            id="ses-sup0001", role="supervisor", pool="opus",
            model="opus", front="alpha", state="running",
        ).to_dict(),
    }})
    monkeypatch.setenv(SESSION_ENV, "ses-sup0001")
    assert cli.main(["front", "queue"]) == 1
    err = capsys.readouterr().err
    assert "role 'supervisor' may not call 'front queue'" in err
