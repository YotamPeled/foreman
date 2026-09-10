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


# -- configured items ------------------------------------------------------

@pytest.fixture()
def spawns(env, monkeypatch):
    """Capture every item start; the double finishes it at once with the
    exit and output the test sets per item, the way the wrapper would:
    output first, then the exit file."""
    calls: list[dict] = []
    exits: dict[str, int] = {}

    def fake_spawn(command, output, exit_path):
        name = Path(output).name.rsplit("-", 1)[0]
        assert Path(output).parent == paths.clockwork_output_dir()
        calls.append({"item": name, "command": command})
        Path(output).write_text(f"ran {command}\n", encoding="utf-8")
        Path(exit_path).write_text(f"{exits.get(name, 0)}\n",
                                   encoding="utf-8")
        return 4_000_000 + len(calls)

    monkeypatch.setattr(clockwork, "spawn_item", fake_spawn)
    return calls, exits


def open_red(item: str) -> list[dict]:
    folded: dict = {}
    for record in store.read_ledger(paths.anomalies_path()):
        folded[(record.get("kind"), record.get("subject"))] = record
    return [r for (kind, subject), r in folded.items()
            if kind == "clockwork red" and subject == item
            and r.get("resolved_at") is None]


def test_nightly_every_24h_runs_once_and_records_exit_and_output(
        env, spawns):
    """Breaks if the item does not run, runs every tick, or loses its
    exit or output: one start, one line, then nothing until 24h pass."""
    calls, _exits = spawns
    write_config(env, '[clockwork]\nnightly = "make nightly"\n'
                      'nightly_every = "24h"\n')
    tick(now=NOW)
    assert calls == [{"item": "nightly", "command": "make nightly"}]
    nightly = lines("nightly")
    assert len(nightly) == 1
    assert nightly[0]["exit"] == 0
    assert nightly[0]["command"] == "make nightly"
    assert nightly[0]["by"] == "collector"
    ref = nightly[0]["output_ref"]
    assert not ref.startswith("/")
    assert (paths.state_dir() / ref).read_text(encoding="utf-8") == \
        "ran make nightly\n"

    tick(now=NOW + timedelta(seconds=2))
    tick(now=NOW + timedelta(hours=23))
    assert len(calls) == 1 and len(lines("nightly")) == 1
    tick(now=NOW + timedelta(hours=24))
    assert len(calls) == 2


def test_a_red_merge_gate_opens_the_anomaly_and_waits_its_interval(
        env, spawns):
    """Breaks if a red item retries on the next tick or opens no anomaly:
    exit 3 is one line and one open 'clockwork merge_gate red'; the item
    runs again only after its hour, and a green run closes the anomaly."""
    calls, exits = spawns
    exits["merge_gate"] = 3
    write_config(env, '[clockwork]\nmerge_gate = "gate"\n'
                      'merge_gate_every = "1h"\n')
    tick(now=NOW)
    assert [r["exit"] for r in lines("merge_gate")] == [3]
    red = open_red("merge_gate")
    assert len(red) == 1
    assert red[0]["detail"].startswith("clockwork merge_gate red: exit 3")

    tick(now=NOW + timedelta(seconds=2))
    tick(now=NOW + timedelta(minutes=59))
    assert len(calls) == 1
    assert len([r for r in store.read_ledger(paths.anomalies_path())
                if r.get("kind") == "clockwork red"]) == 1

    exits["merge_gate"] = 0
    tick(now=NOW + timedelta(hours=1))
    assert len(calls) == 2
    assert [r["exit"] for r in lines("merge_gate")] == [3, 0]
    assert open_red("merge_gate") == []


def test_upgrade_waits_while_a_job_runs_and_says_so(env, spawns, capsys):
    """Breaks if upgrade runs outside the safe point or waits silently."""
    calls, _exits = spawns
    write_config(env, '[clockwork]\nupgrade = "foreman-upgrade"\n'
                      'upgrade_every = "24h"\n')
    jobs = paths.front_jobs_path("f")
    jobs.parent.mkdir(parents=True, exist_ok=True)
    store.append_ledger(jobs, {"id": "job-run1", "state": "running"})
    tick(now=NOW)
    tick(now=NOW + timedelta(seconds=2))
    assert calls == []
    out = capsys.readouterr().out
    assert out.count("upgrade waits: job job-run1 running on f") == 1
    assert cli.main(["clockwork", "list"]) == 0
    assert "waits: job job-run1 running on f" in capsys.readouterr().out

    store.append_ledger(jobs, {"id": "job-run1", "state": "returned"})
    tick(now=NOW + timedelta(seconds=4))
    assert calls == [{"item": "upgrade", "command": "foreman-upgrade"}]


def test_upgrade_waits_while_a_landing_is_open(env, spawns, capsys):
    calls, _exits = spawns
    write_config(env, '[clockwork]\nupgrade = "foreman-upgrade"\n'
                      'upgrade_at = "03:00"\n')
    tree = paths.front_tree_path("f")
    tree.parent.mkdir(parents=True, exist_ok=True)
    store.append_ledger(tree, {"id": "lnd-1", "op": "add", "kind": "job",
                               "role": "script", "lands": "job-a",
                               "state": "queued"})
    tick(now=NOW)
    assert calls == []
    assert "upgrade waits: landing lnd-1 open on f" in capsys.readouterr().out


def test_an_item_with_no_schedule_is_skipped(env, spawns):
    calls, _exits = spawns
    write_config(env, '[clockwork]\nowner_report = "report"\n')
    tick(now=NOW)
    assert calls == []


def test_at_runs_once_per_day_after_its_time(env, spawns):
    calls, _exits = spawns
    write_config(env, '[clockwork]\nowner_report = "report"\n'
                      'owner_report_at = "03:00"\n')
    tick(now=NOW)                                  # 12:00, never run: runs
    tick(now=NOW + timedelta(hours=14))            # 02:00 next day: not yet
    assert len(calls) == 1
    tick(now=NOW + timedelta(hours=15, minutes=1))  # 03:01: runs
    tick(now=NOW + timedelta(hours=16))
    assert len(calls) == 2


def test_clockwork_list_prints_the_table(env, spawns, capsys):
    """Breaks if list drops an item, invents an unconfigured one, or
    misreports the last run and its exit."""
    _calls, exits = spawns
    exits["nightly"] = 2
    write_config(env, '[clockwork]\nnightly = "n"\nnightly_every = "24h"\n'
                      'upgrade = "u"\nupgrade_at = "03:00"\n'
                      'owner_report = "r"\n')
    tick(now=NOW)
    capsys.readouterr()
    assert cli.main(["clockwork", "list"]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.splitlines() == [
        "relaunch       every tick             never run",
        "pool-reset     every tick             never run",
        "clean          every tick, after 24h  never run",
        "dead-sessions  every tick, after 24h  never run",
        "nightly        every 24h              last 2026-09-10 12:00Z exit 2",
        "owner_report   no schedule            never run",
        "upgrade        at 03:00 UTC           last 2026-09-10 12:00Z exit 0",
    ]


def test_clockwork_list_is_refused_to_a_supervisor(env, monkeypatch, capsys):
    seed_roster({"id": "ses-sup", "role": "supervisor", "pool": "claude",
                 "front": "f", "pid": None, "state": "running"})
    monkeypatch.setenv(SESSION_ENV, "ses-sup")
    assert cli.main(["clockwork", "list"]) != 0


def test_the_real_spawn_records_exit_and_output(env):
    """The detached wrapper leaves output and exit where a later tick
    reads them: exit 4 and the command's own words."""
    write_config(env, '[clockwork]\nnightly = "echo from-nightly; exit 4"\n'
                      'nightly_every = "24h"\n')
    import time

    moment = NOW
    for _ in range(100):
        tick(now=moment)
        if lines("nightly"):
            break
        time.sleep(0.05)
        moment += timedelta(seconds=2)
    nightly = lines("nightly")
    assert len(nightly) == 1
    assert nightly[0]["exit"] == 4
    assert (paths.state_dir() / nightly[0]["output_ref"]).read_text(
        encoding="utf-8") == "from-nightly\n"
    assert len(open_red("nightly")) == 1
