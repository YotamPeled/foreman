"""`foreman status` in the v5 shape: queue, capacity, milestones, tree, landings.

Every test drives the real CLI against a fresh FOREMAN_STATE. Front
records are written directly so the tests name the team they need;
``front add`` is not the seam. Capacity's process table is substituted
so the box count does not read this machine.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foreman import capacity, cli, entities, ids, paths, store
from foreman.caller import SESSION_ENV
from foreman.status import NOW_ENV

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
PINNED_NOW = "2026-09-10T12:00:00+00:00"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.setattr(capacity, "_snapshot", lambda: {})
    return tmp_path


def write_v5(name: str, builders: int = 1, pool: str = "grok",
             prefer: int = 0, state: str = "queued") -> dict:
    paths.front_dir(name).mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "grok-4.6", "pool": pool, "model": "grok-4.6",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "grok-4.6", "pool": pool, "model": "grok-4.6",
         "effort": "high", "count": builders, "role": "builder"},
    ]
    line = entities.Front(
        id=ids.mint("front"), name=name, state=state, shape="v5",
        prefer=prefer,
        goal="Show the v5 screen.", finish_line="The queue names both.",
        allocation={"grok": builders}, team=team,
        repositories=[{"name": "foreman", "target": "main",
                       "work": "v5", "base": "main",
                       "url": "https://example.invalid/foreman.git"}],
    ).to_dict()
    store.append_ledger(paths.front_record_path(name), line)
    return line


def status_out(monkeypatch, capsys) -> str:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["status"]) == 0
    return capsys.readouterr().out


def queue_lines(out: str) -> list[str]:
    """The swarm ``queue:`` block, heading included, up to Needs you."""
    lines = out.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == "queue:" or line.startswith("queue: "):
            start = index
            break
    assert start is not None, f"no queue block in:\n{out}"
    end = len(lines)
    for index, line in enumerate(lines[start + 1:], start + 1):
        if line.startswith("Needs you"):
            end = index
            break
    return lines[start:end]


def test_queue_block_names_both_fronts_with_their_reasons(env, monkeypatch,
                                                          capsys):
    """Two queued v5 fronts: the top is team fits, the second is behind it."""
    write_v5("alpha", builders=1)
    write_v5("beta", builders=1)
    out = status_out(monkeypatch, capsys)
    assert queue_lines(out) == [
        "queue:",
        "  alpha  team fits",
        "  beta  behind alpha",
    ]


def test_queue_block_names_a_running_front_after_the_queued_ones(
        env, monkeypatch, capsys):
    """A queued front keeps its wait reason; an active front is running."""
    write_v5("harbor", builders=1, state="queued")
    write_v5("orbit", builders=1, state="active")
    out = status_out(monkeypatch, capsys)
    assert queue_lines(out) == [
        "queue:",
        "  harbor  team fits",
        "  orbit  running",
    ]


def test_capacity_line_prints_held_reserved_and_cap(env, monkeypatch, capsys):
    """After a reserve, Capacity spells held / reserved / cap per pool."""
    write_v5("orbit", builders=1, state="active")
    assert cli.main(["front", "reserve", "orbit"]) == 0
    capsys.readouterr()
    out = status_out(monkeypatch, capsys)
    assert "held 0 / reserved 1 / cap 1" in out
