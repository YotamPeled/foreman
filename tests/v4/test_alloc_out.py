"""A front allocation over an out pool prints the out line, not held.

The break each test catches is in its docstring. Clocks are injected;
nothing writes pool state here, and nothing reads pools.jsonl except
through the reader the pool-out job landed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foreman import capacity, paths, status, store
from foreman.caller import SESSION_ENV


RESET = "2026-09-14T00:00:00Z"
RESET_SHOWN = "2026-09-14 00:00Z"
BEFORE = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
AFTER = datetime(2026, 9, 14, 0, 0, 1, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    return tmp_path


def write_out(pool: str = "grok", reset: str = RESET) -> None:
    store.append_ledger(paths.pools_path(), {
        "id": pool, "pool": pool, "out_until": reset, "because": "quota",
    })


def write_front(name: str, **allocation: int) -> None:
    store.append_ledger(paths.front_record_path(name), {
        "id": f"front-{name}", "name": name, "state": "queued",
        "allocation": allocation,
    })


def test_a_front_allocation_over_an_out_pool_prints_out_not_held(env):
    """Before the reset, both places a front's allocation is rendered
    print the out line and no held count. After it they print the held
    count and no out line. A second role on the same front whose pool
    is not out keeps its held count either side of the reset."""
    write_front("runtime-v4", grok=5, muse=2)
    write_out("grok")
    capacity.grant(pool="grok", front="runtime-v4", role="grok",
                   job="job-held1", session="ses-held0001")

    before_cap = capacity.capacity_lines({}, now=BEFORE)
    before_status = status.render(now=BEFORE)
    out_line = f"  runtime-v4 grok: out until {RESET_SHOWN} (quota)"
    held_line = "  runtime-v4 grok: 1/5 held"
    muse_line = "  runtime-v4 muse: 0/2 held"
    out_alloc = f"grok out until {RESET_SHOWN} (quota)"
    held_alloc = "grok 1/5"

    assert out_line in before_cap
    assert held_line not in before_cap
    assert muse_line in before_cap
    assert out_line in before_status
    assert held_line not in before_status
    assert out_alloc in before_status
    assert held_alloc not in before_status
    assert "muse 0/2" in before_status

    after_cap = capacity.capacity_lines({}, now=AFTER)
    after_status = status.render(now=AFTER)

    assert out_line not in after_cap
    assert held_line in after_cap
    assert muse_line in after_cap
    assert out_line not in after_status
    assert held_line in after_status
    assert out_alloc not in after_status
    assert f"allocation: {held_alloc}, muse 0/2" in after_status


def test_a_front_allocation_that_is_not_out_is_unchanged(env):
    """A front whose pool is not out — no ledger record, or a record
    whose reset has passed — still prints held/ceiling, never an out
    line. That is the line the Capacity tests already pin."""
    write_front("runtime-v4", grok=5)
    capacity.grant(pool="grok", front="runtime-v4", role="grok",
                   job="job-held1", session="ses-held0001")

    lines = capacity.capacity_lines({}, now=BEFORE)
    screen = status.render(now=BEFORE)
    assert "  runtime-v4 grok: 1/5 held" in lines
    assert "out until" not in "\n".join(lines)
    assert "allocation: grok 1/5" in screen
    assert "out until" not in screen
