"""`pool clear` lifts a pool's out mark.

A marked pool refuses a launch; the foreman verb appends the lifting
line, prints the mark it lifted, and the next launch is admitted. A
supervisor is refused naming the role. Tests that would stay green
without the work are not in this file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foreman import capacity, cli, mcp, paths, status, store
from foreman.caller import SESSION_ENV

RESET = "2099-01-01T00:00:00Z"
RESET_SHOWN = "2099-01-01 00:00Z"
NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SID = "ses-mark01"
JOB = "job-mark01"
LINE_NO = 3


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    return tmp_path


def mark_out(pool: str = "grok") -> dict:
    return store.append_ledger(paths.pools_path(), {
        "id": pool, "pool": pool, "out_until": RESET,
        "because": "quota", "session": SID, "job": JOB,
        "record_type": "error", "line_no": LINE_NO,
    })


def roster(session_id: str, role: str, front: str | None = None) -> None:
    store.write_snapshot(paths.roster_path(), {"sessions": {
        session_id: {"id": session_id, "role": role, "front": front,
                     "state": "running"},
    }})


def test_marked_pool_refuses_a_launch(env):
    """An open out mark is a launch refusal that names the pool and the
    reset. Without the mark this assertion has nothing to refuse."""
    mark_out()
    problems = capacity.launch_problems("grok", "grok", "alpha", now=NOW)
    assert problems == [
        "role 'grok' on front 'alpha': pool 'grok' is out until "
        f"{RESET_SHOWN} (quota, error line {LINE_NO})",
    ]


def test_foreman_clear_appends_prints_and_admits(env, capsys, monkeypatch):
    """`pool clear` as the foreman writes out_until null, prints the
    mark's instant and source, and the next launch on the pool is
    admitted."""
    mark_out()
    roster("ses-for00001", "foreman")
    monkeypatch.setenv(SESSION_ENV, "ses-for00001")
    rc = cli.main(["pool", "clear", "grok", "--reason", "false detection"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert captured.out.strip() == (
        f"pool grok cleared (was out until {RESET_SHOWN}, because quota, "
        f"from {SID} line {LINE_NO})"
    )
    folded = store.fold_by_id(store.read_ledger(paths.pools_path()))
    assert len(folded) == 1
    line = folded[0]
    assert line["id"] == "grok"
    assert line["pool"] == "grok"
    assert line["out_until"] is None
    assert line["because"] == "cleared: false detection"
    assert line["cleared"] == {"session": SID, "job": JOB}
    assert line["by"] == "ses-for00001"
    assert line.get("at")
    assert capacity.launch_problems("grok", "grok", "alpha", now=NOW) == []


def test_supervisor_is_refused_naming_the_role(env, capsys, monkeypatch):
    """A supervisor may not lift a mark; the refusal names the role."""
    mark_out()
    roster("ses-sup00001", "supervisor", front="comp")
    monkeypatch.setenv(SESSION_ENV, "ses-sup00001")
    rc = cli.main(["pool", "clear", "grok", "--reason", "false detection"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "role 'supervisor' may not call 'pool clear'" in err
    folded = store.fold_by_id(store.read_ledger(paths.pools_path()))
    assert folded[0]["out_until"] == RESET


def test_unmarked_pool_is_refused_naming_it(env, capsys, monkeypatch):
    """Clearing a pool that is not out names the pool."""
    roster("ses-for00001", "foreman")
    monkeypatch.setenv(SESSION_ENV, "ses-for00001")
    rc = cli.main(["pool", "clear", "grok", "--reason", "false detection"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "pool grok is not out" in err
    try:
        lines = store.read_ledger(paths.pools_path())
    except OSError:
        lines = []
    assert lines == []


def test_unknown_pool_is_refused_naming_the_known_ones(
        env, capsys, monkeypatch):
    """An unknown pool is refused with the known names, not as 'not out'."""
    roster("ses-for00001", "foreman")
    monkeypatch.setenv(SESSION_ENV, "ses-for00001")
    rc = cli.main(["pool", "clear", "nope", "--reason", "false detection"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "unknown pool 'nope'" in err
    assert "known pools:" in err
    assert "grok" in err


def test_status_shows_the_cleared_line_for_the_hour(
        env, capsys, monkeypatch):
    """After a clear, status prints ``cleared <age> ago (<reason>)``
    under the pool for the rest of the hour, then drops it."""
    mark_out()
    roster("ses-for00001", "foreman")
    monkeypatch.setenv(SESSION_ENV, "ses-for00001")
    assert cli.main(["pool", "clear", "grok", "--reason",
                     "false detection"]) == 0
    capsys.readouterr()
    line = store.fold_by_id(store.read_ledger(paths.pools_path()))[0]
    at = datetime.fromisoformat(str(line["at"]).replace("Z", "+00:00"))
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    screen = status.render(now=at + timedelta(minutes=5))
    rows = screen.splitlines()
    grok = next(i for i, row in enumerate(rows) if row.startswith("  grok:"))
    assert rows[grok + 1] == "    cleared 5m ago (false detection)"
    later = status.render(now=at + timedelta(minutes=61))
    assert "cleared 5m ago (false detection)" not in later
    assert "    cleared " not in later


def test_mcp_carries_pool_clear_for_the_foreman_only(env, monkeypatch):
    """The verb is in the foreman's MCP tool row and not the supervisor's."""
    roster("ses-for00001", "foreman")
    monkeypatch.setenv(SESSION_ENV, "ses-for00001")
    listed = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "pool_clear" in names
    store.write_snapshot(paths.roster_path(), {"sessions": {
        "ses-sup00001": {"id": "ses-sup00001", "role": "supervisor",
                         "front": "comp", "state": "running"},
    }})
    monkeypatch.setenv(SESSION_ENV, "ses-sup00001")
    listed = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "pool_clear" not in names
