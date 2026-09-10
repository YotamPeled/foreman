"""Quota asks: the collector files one when the top front waits and
the machine has room.

The world caps a fake pool; ``[caps] max`` is the raise ceiling. Front
records are written directly so the tests name the team they need.
Worker and supervisor spawn is substituted: nothing here reaches systemd.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foreman import cli, entities, fronts, ids, paths, store
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.entities import InboxItem, Session
from foreman.pools import LaunchContext, PoolAdapter
from foreman.pools import _common

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


class FakeAdapter(PoolAdapter):
    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    def launch(self, ctx: LaunchContext) -> int:
        raise AssertionError("quota-ask tests do not spawn")

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
    write_pool_config(cap=3, max_cap=6)
    monkeypatch.setattr(_common, "stop_unit", lambda *a, **k: False)
    try:
        yield tmp_path
    finally:
        pools.unregister("fake")


def write_pool_config(*, cap: int, max_cap: int | None) -> None:
    text = f"[pool.fake]\ncap = {cap}\nroles = [\"muse\"]\n"
    if max_cap is not None:
        text += f"\n[caps]\nmax = {max_cap}\n"
    paths.config_file().write_text(text, encoding="utf-8")


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


def inbox_items() -> list[InboxItem]:
    return [InboxItem.from_dict(rec)
            for rec in store.read_ledger(paths.inbox_path())
            if isinstance(rec, dict)]


def open_asks() -> list[InboxItem]:
    return [item for item in inbox_items() if item.answered_at is None]


def hold(name: str, builders: int) -> None:
    write_v5(name, builders=builders, state="active")
    assert cli.main(["front", "reserve", name]) == 0


def test_one_tick_files_exactly_one_quota_ask(env, capsys):
    """A top front wanting 3 on a pool with cap 3 and 2 reserved by
    another front, max 6: one tick files exactly one ask with those
    numbers and the recommendation."""
    hold("held", builders=2)
    capsys.readouterr()
    write_v5("alpha", builders=3)
    tick(now=NOW)
    asks = open_asks()
    assert len(asks) == 1
    item = asks[0]
    assert item.kind == "money"
    assert item.question == (
        "front alpha waits on pool fake: cap 3, reserved 2 by held, "
        "wants 3. Raise the cap to 5 until alpha ends?")
    assert item.recommendation == "raise to 5"
    assert item.options == ["raise to 5", "wait", "stop held"]
    record = fronts.read_front_record("alpha")
    assert record is not None
    assert record.get("quota_ask") == {"fake": item.id}


def test_a_second_tick_files_none(env, capsys):
    """The same top front does not get a second ask on the next tick."""
    hold("held", builders=2)
    capsys.readouterr()
    write_v5("alpha", builders=3)
    tick(now=NOW)
    tick(now=NOW + timedelta(seconds=2))
    assert len(open_asks()) == 1
    assert len(inbox_items()) == 1


def test_no_room_files_no_ask_and_the_queue_says_so(env, capsys):
    """With max 3 equal to the cap, no ask is filed and the queue
    prints ``no room``."""
    write_pool_config(cap=3, max_cap=3)
    hold("held", builders=2)
    capsys.readouterr()
    write_v5("alpha", builders=3)
    tick(now=NOW)
    assert open_asks() == []
    assert inbox_items() == []
    assert fronts.read_front_record("alpha").get("quota_ask") in (None, {})
    assert cli.main(["front", "queue"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "alpha  no room: cap 3 is the maximum",
    ]
