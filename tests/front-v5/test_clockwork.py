"""The collector's clockwork items, each a line of clockwork.jsonl.

Every test runs the real collector tick against a fresh FOREMAN_STATE and
FOREMAN_CONFIG. The disk door (``remove_session_files``), the item spawn
door (``spawn_item``) and the relaunch verb are replaced where a test
would otherwise remove a real checkout, start a command or summon a
session; one test drives each real door against a throwaway repository.
Sessions are seeded with no pid, so no tick asks systemd about a unit.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import capacity, cli, clockwork, paths, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.collector import tick

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    paths.ensure_state_tree()
    (tmp_path / "config").mkdir()
    return tmp_path


def iso(moment: datetime) -> str:
    return moment.isoformat()


def lines(item: str | None = None) -> list[dict]:
    records = store.read_ledger(paths.clockwork_path())
    return [r for r in records if item is None or r.get("item") == item]


def write_config(env: Path, body: str) -> None:
    (env / "config" / "foreman.toml").write_text(body, encoding="utf-8")


def seed_roster(*entries: dict) -> None:
    store.write_snapshot(paths.roster_path(),
                         {"sessions": {e["id"]: e for e in entries}})


def roster() -> dict:
    return store.read_snapshot(paths.roster_path())["sessions"]


def worker(sid: str, worktree: str, exited_at: str | None,
           state: str = "exited") -> dict:
    entry = {"id": sid, "role": "grok", "pool": "grok", "front": "f",
             "job": None, "pid": None, "worktree": worktree,
             "branch": f"job/{sid}", "state": state,
             "started_at": iso(NOW - timedelta(days=3))}
    if exited_at is not None:
        entry["exited_at"] = exited_at
    return entry


# -- pool-reset ------------------------------------------------------------

def test_a_pool_past_its_reset_gets_one_line_and_launches_again(env):
    """Breaks if the reset line is not written, is written every tick, or
    the pool stays out: a pool out until an hour ago is cleared once."""
    store.append_ledger(paths.pools_path(), {
        "id": "grok", "pool": "grok",
        "out_until": iso(NOW - timedelta(hours=1)), "because": "quota"})
    assert capacity.pool_out("grok", NOW - timedelta(hours=2)) is not None

    tick(now=NOW)

    pools = store.read_ledger(paths.pools_path())
    assert len(pools) == 2
    assert pools[-1]["out_until"] is None
    assert pools[-1]["because"] == "reset reached"
    assert pools[-1]["by"] == "collector"
    # Folded, the pool is back even measured against the old instant.
    assert capacity.pool_out("grok", NOW - timedelta(hours=2)) is None
    assert not any("is out" in p for p in capacity.launch_problems(
        "grok", "grok", None, now=NOW, table={}))
    reset = lines("pool-reset")
    assert len(reset) == 1
    assert reset[0]["exit"] == 0 and reset[0]["pool"] == "grok"

    tick(now=NOW + timedelta(seconds=2))
    assert len(store.read_ledger(paths.pools_path())) == 2
    assert len(lines("pool-reset")) == 1


def test_a_pool_still_out_is_left_alone(env):
    store.append_ledger(paths.pools_path(), {
        "id": "grok", "pool": "grok",
        "out_until": iso(NOW + timedelta(hours=1)), "because": "quota"})
    tick(now=NOW)
    assert len(store.read_ledger(paths.pools_path())) == 1
    assert lines("pool-reset") == []


# -- clean and dead-sessions ----------------------------------------------

def test_an_old_exited_worktree_is_removed_and_a_fresh_one_is_not(
        env, monkeypatch):
    """Breaks if clean ignores clean_after or skips the old one: only the
    worktree exited 25h ago goes, and only its entry becomes history."""
    removed: list[str] = []
    monkeypatch.setattr(clockwork, "remove_session_files",
                        lambda sid, record: removed.append(sid))
    old_wt, fresh_wt = env / "wt-old", env / "wt-fresh"
    old_wt.mkdir()
    fresh_wt.mkdir()
    seed_roster(worker("ses-old", str(old_wt), iso(NOW - timedelta(hours=25))),
                worker("ses-new", str(fresh_wt), iso(NOW - timedelta(hours=1))))

    tick(now=NOW)

    assert removed == ["ses-old"]
    sessions = roster()
    assert sessions["ses-old"]["state"] == "history"
    assert sessions["ses-new"]["state"] == "exited"
    clean = lines("clean")
    assert len(clean) == 1
    assert clean[0]["count"] == 1 and clean[0]["sessions"] == ["ses-old"]
    assert clean[0]["exit"] == 0
    assert "1 worktree" in clean[0]["command"]
    dead = lines("dead-sessions")
    assert len(dead) == 1 and dead[0]["sessions"] == ["ses-old"]

    tick(now=NOW + timedelta(seconds=2))
    assert removed == ["ses-old"]
    assert len(lines("clean")) == 1 and len(lines("dead-sessions")) == 1


def test_clean_after_comes_from_the_config(env, monkeypatch):
    removed: list[str] = []
    monkeypatch.setattr(clockwork, "remove_session_files",
                        lambda sid, record: removed.append(sid))
    write_config(env, '[clockwork]\nclean_after = "30m"\n')
    wt = env / "wt"
    wt.mkdir()
    seed_roster(worker("ses-a", str(wt), iso(NOW - timedelta(hours=1))))
    tick(now=NOW)
    assert removed == ["ses-a"]


def test_an_exited_session_is_stamped_then_aged_from_the_stamp(
        env, monkeypatch):
    """An exited entry with no exit time is stamped, not cleaned: its age
    starts the tick that first saw it exited."""
    removed: list[str] = []
    monkeypatch.setattr(clockwork, "remove_session_files",
                        lambda sid, record: removed.append(sid))
    wt = env / "wt"
    wt.mkdir()
    seed_roster(worker("ses-a", str(wt), None))
    tick(now=NOW)
    assert roster()["ses-a"]["exited_at"] == iso(NOW)
    assert removed == []
    tick(now=NOW + timedelta(hours=24, seconds=1))
    assert removed == ["ses-a"]


def test_the_real_disk_door_removes_the_worktree_and_keeps_the_branch(env):
    repo, wt = env / "repo", env / "wt-job"
    git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b",
                    "job/x", str(wt)], check=True)
    scratch = paths.session_scratch_dir("ses-x")
    scratch.mkdir(parents=True)
    (scratch / "note").write_text("x", encoding="utf-8")

    assert clockwork.remove_session_files(
        "ses-x", {"worktree": str(wt), "branch": "job/x"}) is None
    assert not wt.exists()
    assert not scratch.exists()
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list",
                               "job/x"], capture_output=True, text=True)
    assert "job/x" in branches.stdout
    # A main checkout is never removed.
    assert "main checkout" in clockwork.remove_session_files(
        "ses-y", {"worktree": str(repo), "branch": "main"})
    assert repo.is_dir()


# -- relaunch --------------------------------------------------------------

def test_the_dead_supervisor_relaunch_is_an_item_line(env, monkeypatch):
    """Breaks if the relaunch stops being recorded: one line, the verb
    it ran and the exit the launcher answered."""
    calls: list[str] = []

    def fake_relaunch(sid, by=None, quiet=False):
        calls.append(sid)
        return 0

    monkeypatch.setattr(launch_module, "relaunch_main", fake_relaunch)
    seed_roster({"id": "ses-sup", "role": "supervisor", "pool": "claude",
                 "front": "f", "pid": 3_999_999, "pid_starttime": 1,
                 "worktree": "", "state": "running",
                 "started_at": iso(NOW - timedelta(hours=1))})
    tick(now=NOW)
    assert calls == ["ses-sup"]
    relaunch = lines("relaunch")
    assert len(relaunch) == 1
    assert relaunch[0]["command"] == "foreman relaunch ses-sup"
    assert relaunch[0]["exit"] == 0
    assert relaunch[0]["by"] == "collector"
    assert relaunch[0]["at"] == iso(NOW)
