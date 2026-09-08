"""Pool caps, front allocations, slot grants and the Capacity block.

Every test drives the real entry points — ``foreman launch``, ``foreman
cap``, ``foreman status`` and a real collector tick — against a fresh
FOREMAN_STATE and FOREMAN_CONFIG under tmp_path, then reads the ledgers
back. Nothing here writes to a real state or config directory and nothing
calls the release function directly: a slot comes back because the
collector saw what happened to the process, which is the only way it comes
back in production.

The break each test catches is in its docstring. The ones worth naming up
front: a ceiling that behaves like a reservation (holding a slot for an
idle front), a held count read from a stored number instead of the open
grants, a job that stays "running" forever after its process is gone, and
a config file that crashes or silently reads zero instead of falling back.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import capacity, cli, config, paths, store
from foreman.caller import SESSION_ENV
from foreman.collector import CollectorConfig, tick
from foreman.entities import JOB_ROLES, Session
from foreman.pools import LaunchContext, PoolAdapter

#: A brief that validates, allocating two muse and one opus. Two roles is
#: the point: the ceiling keys on the role, the cap keys on the pool, and a
#: test that used one name for both could not tell them apart.
BRIEF = '''name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 2
opus = 1

[[task]]
title = "first work"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "make check first"
size = 3
after = []
'''

SPEC = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests -q\n"
)


class FakeAdapter(PoolAdapter):
    """A pool that starts a real, harmless child so the collector has a
    process identity to observe, and answers ``observe`` from a script."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    script: dict[str, dict] = {}
    children: dict[str, subprocess.Popen] = {}

    def launch(self, ctx: LaunchContext) -> int:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        type(self).children[ctx.session.id or ""] = proc
        ctx.pid_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
        return proc.pid

    def observe(self, session: Session) -> dict:
        return dict(self.script.get(session.id or "", {
            "transcript_mtime": None, "cpu_s": 0.0,
            "finish_present": False, "finish_rc": None,
        }))

    def command_str(self, ctx: LaunchContext) -> str:
        return "fake-exec"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from foreman import pools

    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    FakeAdapter.script = {}
    FakeAdapter.children = {}
    pools.register("fake", FakeAdapter())
    _git(tmp_path, "init", "-b", "main", "-q", str(tmp_path))
    _git(tmp_path, "-C", str(tmp_path), "commit", "-q", "--allow-empty",
         "-m", "init")
    (tmp_path / "spec.md").write_text(SPEC, encoding="utf-8")
    try:
        yield tmp_path
    finally:
        pools.unregister("fake")
        for proc in FakeAdapter.children.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.invalid",
         "-c", "user.name=foreman-test", *args],
        cwd=str(root), check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)


def add_front(root: Path, name: str) -> None:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(BRIEF.format(name=name),
                                          encoding="utf-8")
    assert cli.main(["front", "add", str(brief_dir)]) == 0


def launch(root: Path, *extra: str, role: str = "muse", pool: str = "fake",
           front: str = "alpha") -> int:
    return cli.main(["launch", role, pool, str(root / "spec.md"),
                     "--repo", str(root), "--front", front,
                     "--task", "first work", *extra])


def session_of(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("session: "):
            return line.split("session: ", 1)[1].strip()
    raise AssertionError(f"no session line in:\n{out}")


def launched(root: Path, capsys, **kwargs) -> str:
    assert launch(root, **kwargs) == 0
    return session_of(capsys.readouterr().out)


def refused(root: Path, capsys, **kwargs) -> str:
    rc = launch(root, **kwargs)
    out, err = capsys.readouterr()
    assert rc != 0, f"launch was allowed:\n{out}"
    return err


def job_of(session_id: str) -> str:
    roster = store.read_snapshot(paths.roster_path(), default={"sessions": {}})
    return roster["sessions"][session_id]["job"]


def job_lines(job_id: str, front: str = "alpha") -> list[dict]:
    return [line for line in
            store.read_ledger(paths.front_jobs_path(front))
            if line.get("id") == job_id]


def kill_session_process(session_id: str) -> None:
    proc = FakeAdapter.children[session_id]
    proc.kill()
    proc.wait()


def ledger_lines() -> int:
    return len(store.read_ledger(paths.slots_path()))


def held(pool: str = "fake") -> int:
    return capacity.held_by_pool().get(pool, 0)


def run_tick(now: datetime | None = None) -> None:
    tick(now=now or datetime.now(timezone.utc),
         config=CollectorConfig(vendor_markers=("no-such-vendor",)))


# --------------------------------------------------------------------------
# The two checks
# --------------------------------------------------------------------------


def test_the_front_ceiling_refuses_the_launch_past_it(env, capsys):
    """A front holding its whole allocation of a role is refused the next
    one, and the refusal carries the numbers: the count it saw and the
    ceiling it hit. A refusal that only says "no" leaves the supervisor
    guessing which of the two limits stopped it."""
    add_front(env, "alpha")
    capsys.readouterr()
    launched(env, capsys)
    launched(env, capsys)

    err = refused(env, capsys)

    assert "role 'muse' on front 'alpha': 2 held, ceiling 2" in err
    assert "pool" not in err


def test_the_pool_cap_refuses_the_launch_past_it(env, capsys):
    """The pool caps the whole system, so a front well inside its own
    ceiling is still refused when the pool has nothing left, and the
    refusal names the pool, the count and the cap."""
    add_front(env, "alpha")
    assert cli.main(["cap", "fake", "1"]) == 0
    capsys.readouterr()
    launched(env, capsys)

    err = refused(env, capsys)

    assert "role 'muse' on front 'alpha': 1 held in pool 'fake', cap 1" in err
    assert "ceiling" not in err


def test_a_launch_below_the_ceiling_is_allowed(env, capsys):
    """The ceiling is a ceiling, not a reservation: a front allocated two
    of a role and holding one gets the second. Code that reserved the
    allocation up front — or compared held to ceiling with the wrong
    inequality — refuses here."""
    add_front(env, "alpha")
    capsys.readouterr()
    first = launched(env, capsys)
    assert held() == 1

    second = launched(env, capsys)

    assert second != first
    assert held() == 2


def test_a_front_with_no_record_is_checked_against_the_pool_cap_alone(
        env, capsys):
    """Nothing on the ledger said what this front may hold, so there is no
    ceiling to check and only the pool speaks. Treating a missing record as
    a ceiling of zero would refuse every launch on it."""
    assert cli.main(["cap", "fake", "1"]) == 0
    capsys.readouterr()
    assert capacity.ceiling("ghost", "muse") is None
    launched(env, capsys, front="ghost")

    err = refused(env, capsys, front="ghost")

    assert "role 'muse' on front 'ghost': 1 held in pool 'fake', cap 1" in err
    assert "ceiling" not in err


def test_a_dry_run_runs_every_check_and_grants_no_slot(env, capsys):
    """A dry run answers "would this be allowed?", so it is refused exactly
    where a real launch would be — and it takes nothing, so asking twice
    does not exhaust the pool."""
    add_front(env, "alpha")
    assert cli.main(["cap", "fake", "1"]) == 0
    capsys.readouterr()

    assert launch(env, "--dry-run") == 0
    capsys.readouterr()
    assert launch(env, "--dry-run") == 0
    capsys.readouterr()
    assert held() == 0
    assert ledger_lines() == 0

    launched(env, capsys)
    err = refused(env, capsys, front="ghost")
    assert "role 'muse' on front 'ghost': 1 held in pool 'fake', cap 1" in err
    assert launch(env, "--dry-run", front="ghost") != 0
    assert "role 'muse' on front 'ghost': 1 held in pool 'fake', cap 1" in capsys.readouterr().err


def test_two_launches_racing_for_one_slot_do_not_both_get_it(env, capsys):
    """Check-and-grant is one operation, so a cap of one hands out one slot
    however the launches are timed.

    Two launchers released together both read an empty pool while the other
    was already on its way to filling it, and both got a worker: the check
    ran before the spawn and the grant after it, with nothing holding the
    two together. That is the worst defect this file can carry, because
    every screen afterwards says the swarm is inside a budget it is not.
    """
    add_front(env, "alpha")
    assert cli.main(["cap", "fake", "1"]) == 0
    capsys.readouterr()
    together = threading.Barrier(2)
    codes: list[int] = []

    def race() -> None:
        together.wait()
        codes.append(launch(env))

    racers = [threading.Thread(target=race) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(timeout=120)
    err = capsys.readouterr().err

    assert sorted(codes) == [0, 1], f"a cap of one admitted {codes}"
    assert "role 'muse' on front 'alpha': 1 held in pool 'fake', cap 1" in err, \
        "the loser must be refused by the cap, not by an accident"
    assert held() == 1
    assert len(capacity.open_grants()) == 1


def test_a_launch_over_both_limits_is_told_both_of_them(env, capsys):
    """One refusal carries every limit the launch broke, each with its own
    numbers. Reporting only the first would send the supervisor to raise the
    ceiling and hit the cap on the very next try."""
    add_front(env, "alpha")
    assert cli.main(["cap", "fake", "2"]) == 0
    capsys.readouterr()
    launched(env, capsys)
    launched(env, capsys)

    err = refused(env, capsys)

    assert "role 'muse' on front 'alpha': 2 held, ceiling 2" in err
    assert "role 'muse' on front 'alpha': 2 held in pool 'fake', cap 2" in err


def test_the_opus_cap_stops_an_opus_worker(env, capsys):
    """The cap the owner sets on Opus governs the pool Opus workers run on.

    The packaged default configured `[pool.opus]`, the adapter registers
    itself as `claude`, and a launch is checked against the pool it names,
    so `cap opus 0` wrote a table nothing ever read and the limit on the
    most expensive model on the machine held nothing back.
    """
    assert cli.main(["cap", "opus", "0"]) == 0
    out = capsys.readouterr().out

    rc = cli.main(["launch", "opus", "claude", str(env / "spec.md"),
                   "--repo", str(env), "--front", "ghost",
                   "--task", "first work", "--dry-run"])
    err = capsys.readouterr().err

    assert "claude: cap 0" in out
    assert rc != 0
    assert ("role 'opus' on front 'ghost': 0 held in pool 'claude', cap 0"
            in err)
    assert config.load().cap("claude") == 0
    assert "[pool.opus]" not in paths.config_file().read_text(encoding="utf-8")


def _with_supervisor_cap(count: int) -> None:
    """The packaged configuration with the supervisor cap set to ``count``."""
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        config.packaged_text().replace("supervisor_cap = 2",
                                       f"supervisor_cap = {count}"),
        encoding="utf-8")


def summon(root: Path, front: str) -> int:
    return cli.main(["launch", "supervisor", front, "--repo", str(root),
                     "--dry-run"])


def test_the_supervisor_cap_refuses_a_summon_past_it(env, capsys):
    """`supervisor_cap` was loaded and never read: summoning checked only
    that the front had none already, so the owner's limit on how many
    supervisors may run at once bounded nothing at all."""
    add_front(env, "alpha")
    _with_supervisor_cap(0)
    capsys.readouterr()

    rc = summon(env, "alpha")
    err = capsys.readouterr().err

    assert rc != 0
    assert ("role 'supervisor' on front 'alpha': 0 held across every front, "
            "supervisor cap 0 on pool 'claude'") in err


def test_the_supervisor_cap_counts_supervisors_on_every_front(env, capsys):
    """The cap is the system's, not the front's: a front with no supervisor
    of its own is still refused one when the machine is at its limit. A
    count filtered to the front being summoned would let every front on the
    machine have one however low the cap was set."""
    add_front(env, "alpha")
    add_front(env, "beta")
    _with_supervisor_cap(1)
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: roster["sessions"].__setitem__(
            "ses-alpha001", Session(id="ses-alpha001", role="supervisor",
                                    pool="opus", front="alpha",
                                    state="running").to_dict()) or roster,
        default={"sessions": {}})
    capsys.readouterr()

    rc = summon(env, "beta")
    err = capsys.readouterr().err

    assert rc != 0
    assert ("role 'supervisor' on front 'beta': 1 held across every front, "
            "supervisor cap 1 on pool 'claude'") in err
    assert "already has a live supervisor" not in err, \
        "beta has none of its own; this must be the system's cap"


# --------------------------------------------------------------------------
# Where a slot comes back
# --------------------------------------------------------------------------


def _at_the_cap(env, capsys) -> str:
    """One front, a pool cap of one, and the one slot taken."""
    add_front(env, "alpha")
    assert cli.main(["cap", "fake", "1"]) == 0
    capsys.readouterr()
    sid = launched(env, capsys)
    assert held() == 1
    assert "role 'muse' on front 'alpha': 1 held in pool 'fake', cap 1" in refused(env, capsys)
    return sid


def test_a_returned_job_gives_its_slot_back(env, capsys):
    """The collector reads the finish marker, marks the job returned and
    releases the grant in the same breath. Without the release the pool
    stays full forever after its first job finishes."""
    sid = _at_the_cap(env, capsys)
    job = job_of(sid)
    FakeAdapter.script[sid] = {"transcript_mtime": None, "cpu_s": 1.0,
                               "finish_present": True, "finish_rc": 0}
    kill_session_process(sid)

    run_tick()

    assert job_lines(job)[-1]["state"] == "returned"
    assert held() == 0
    grant = capacity.folded_grants()[-1]
    assert grant["released_because"] == "returned"
    launched(env, capsys)


def test_a_timeout_kill_gives_its_slot_back(env, capsys):
    """A job killed for running past its timeout releases its slot too: the
    pool must not stay full because a worker had to be stopped."""
    sid = _at_the_cap(env, capsys)
    job = job_of(sid)

    run_tick(now=datetime.now(timezone.utc) + timedelta(hours=2))

    roster = store.read_snapshot(paths.roster_path())
    assert roster["sessions"][sid]["state"] == "killed"
    assert job_lines(job)[-1]["state"] == "failed"
    assert held() == 0
    assert capacity.folded_grants()[-1]["released_because"] == "killed"
    launched(env, capsys)


def test_a_process_gone_with_no_marker_is_killed_and_gives_its_slot_back(
        env, capsys):
    """The process is gone and the log carries no finish marker, so the job
    did not return: it becomes killed on the next tick and its slot comes
    back. Leaving it "running" is the lie the collector exists to prevent —
    a screen showing work that stopped an hour ago, and a pool that never
    frees the slot it was holding for it."""
    sid = _at_the_cap(env, capsys)
    job = job_of(sid)
    kill_session_process(sid)

    run_tick()

    latest = job_lines(job)[-1]
    assert latest["state"] == "killed"
    assert "by" not in latest, "nobody stopped it; the record must name nobody"
    assert held() == 0
    assert capacity.folded_grants()[-1]["released_because"] == "killed"
    launched(env, capsys)


def test_a_killed_job_names_the_session_that_stopped_it(env, capsys):
    """Where a Foreman verb did the stopping, the verb leaves its session id
    on the roster record and the killed job line names it. A record that
    said only "killed" would lose the one fact the owner asks first: who."""
    add_front(env, "alpha")
    capsys.readouterr()
    stopper = "ses-stopper1"
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: roster["sessions"].__setitem__(
            stopper, Session(id=stopper, role="supervisor", pool="fake",
                             front="alpha", state="running").to_dict())
        or roster,
        default={"sessions": {}})
    sid = launched(env, capsys)
    job = job_of(sid)
    kill_session_process(sid)
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: roster["sessions"][sid].update(
            {"state": "killed", "killed_by": stopper}) or roster,
        default={"sessions": {}})

    run_tick()

    latest = job_lines(job)[-1]
    assert latest["state"] == "killed"
    assert latest["by"] == stopper


def test_a_release_is_an_appended_line_and_the_fold_carries_it(env, capsys):
    """Every ledger is append-only: releasing a grant appends a revised copy
    of its own line and edits no byte already written. Code that rewrote the
    grant in place would leave the line count unchanged and lose the moment
    the slot was taken."""
    sid = _at_the_cap(env, capsys)
    before = store.read_ledger(paths.slots_path())
    assert len(before) == 1
    granted_at = before[0]["granted_at"]
    kill_session_process(sid)

    run_tick()

    after = store.read_ledger(paths.slots_path())
    assert len(after) == 2
    assert after[0] == before[0], "the first line was rewritten"
    folded = capacity.folded_grants()
    assert len(folded) == 1, "the release must fold onto the grant, not add one"
    assert folded[0]["id"] == before[0]["id"]
    assert folded[0]["granted_at"] == granted_at
    assert folded[0]["released_at"] is not None
    assert capacity.open_grants() == []


def test_a_second_tick_does_not_release_a_released_grant(env, capsys):
    """A released grant is released once. A tick that appended again would
    grow the ledger without end for every finished job on the machine."""
    sid = _at_the_cap(env, capsys)
    kill_session_process(sid)
    run_tick()
    lines = ledger_lines()

    run_tick()

    assert ledger_lines() == lines


# --------------------------------------------------------------------------
# The configuration
# --------------------------------------------------------------------------


def test_the_packaged_default_is_copied_into_an_existing_config_dir(env):
    """First read with a config directory and no file: the packaged default
    lands there, so the owner has a file to edit that says what is shipped."""
    paths.config_dir().mkdir(parents=True, exist_ok=True)

    settings = config.load()

    assert paths.config_file().is_file()
    assert paths.config_file().read_text(encoding="utf-8") == \
        config.packaged_text()
    assert settings.cap("muse") == 5
    assert settings.cap("claude") == 2
    assert settings.supervisor_cap("claude") == 2


def test_a_user_file_overrides_one_pool_and_leaves_the_rest(env):
    """The user's file overrides key by key, not wholesale: a file naming
    one pool must not wipe the caps of the three it does not mention."""
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[pool.muse]\ncap = 9\n", encoding="utf-8")

    settings = config.load()

    assert settings.cap("muse") == 9
    assert settings.cap("grok") == 1
    assert settings.cap("codex") == 1
    assert settings.supervisor_cap("claude") == 2


def test_a_user_file_that_does_not_parse_falls_back_and_warns(env, capsys):
    """A broken config is not a crash and not a silent zero: the packaged
    defaults answer and one warning on stderr names the file, so the owner
    can find it. A cap of zero read from a broken file would refuse every
    launch on the machine with no explanation."""
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[pool.muse\ncap = ??\n", encoding="utf-8")

    settings = config.load()

    err = capsys.readouterr().err
    assert str(paths.config_file()) in err
    assert settings.user_read is False
    assert settings.cap("muse") == 5
    assert settings.cap("grok") == 1


def test_cap_changes_what_the_next_launch_allows(env, capsys):
    """The owner's verb is the whole point: raising the cap lets the launch
    that was just refused through, and lowering it stops the next one."""
    add_front(env, "alpha")
    assert cli.main(["cap", "fake", "1"]) == 0
    assert "fake: cap 1" in capsys.readouterr().out
    launched(env, capsys)
    assert "role 'muse' on front 'alpha': 1 held in pool 'fake', cap 1" in refused(env, capsys)

    assert cli.main(["cap", "fake", "2"]) == 0
    capsys.readouterr()

    launched(env, capsys)
    assert held() == 2
    assert "role 'muse' on front 'alpha': 2 held, ceiling 2" in \
        refused(env, capsys)


def test_cap_refuses_an_unknown_pool_and_a_negative_number(env, capsys):
    """Both violations named at once, and neither reaches the file: a typo
    that quietly created a table for a pool nothing can launch would leave
    the owner reading a cap that does nothing."""
    assert cli.main(["cap", "muse", "3"]) == 0
    capsys.readouterr()

    assert cli.main(["cap", "nosuchpool", "-2"]) == 1
    err = capsys.readouterr().err

    assert "unknown pool 'nosuchpool'" in err
    assert "negative" in err
    assert "nosuchpool" not in paths.config_file().read_text(encoding="utf-8")
    assert config.load().cap("muse") == 3


def test_cap_keeps_every_other_setting_in_the_file(env, capsys):
    """The file is shared with the collector's thresholds, so `cap` edits
    the one line it owns. Re-rendering the file would drop settings the
    owner wrote for another reader."""
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        "[collector]\ntick_seconds = 7\n\n[pool.muse]\ncap = 4\n",
        encoding="utf-8")

    assert cli.main(["cap", "muse", "6"]) == 0
    capsys.readouterr()

    text = paths.config_file().read_text(encoding="utf-8")
    assert "tick_seconds = 7" in text
    assert config.load().cap("muse") == 6


def test_every_registered_pool_has_a_cap_and_every_cap_names_a_pool(env):
    """The shipped configuration and the adapter registry name the same
    pools, and every worker role resolves to one of them. A table for a pool
    that does not exist is a cap nothing checks; a pool with no table is a
    model family the owner cannot limit at all."""
    from foreman import pools

    registered = set(pools.names()) - {FakeAdapter.name}
    shipped = config.defaults()

    assert set(shipped) == registered
    for name in registered:
        assert config.load().cap(name) is not None, f"{name} has no cap"
    for role in JOB_ROLES + ("supervisor",):
        assert capacity.pool_for_role(role) in registered, role
    assert capacity.pool_for_role("opus") == "claude"
    assert capacity.pool_for_role("astra") == "codex"


def test_cap_rewrites_a_table_whose_header_carries_a_comment(env, capsys):
    """`cap` matched an exact `[pool.<name>]` line, so a header with a
    trailing comment looked like no table at all and a second one was
    appended. The file then failed to load, the packaged defaults answered,
    and the cap the owner had just lowered read *higher* than before — a
    shutdown that raised the limit."""
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    before = ("# the caps I run with\n"
              "[collector]\n"
              "tick_seconds = 7  # every seven seconds\n"
              "\n"
              "[pool.muse] # the writers\n"
              "cap = 4\n"
              "supervisor_cap = 1\n")
    paths.config_file().write_text(before, encoding="utf-8")

    assert cli.main(["cap", "muse", "0"]) == 0
    capsys.readouterr()

    after = paths.config_file().read_text(encoding="utf-8")
    assert [line for line in after.splitlines()
            if line.startswith("[pool.muse")] == ["[pool.muse] # the writers"]
    assert [line for line in before.splitlines()
            if not line.startswith("cap =")] == \
        [line for line in after.splitlines()
         if not line.startswith("cap =")], \
        "every line but the cap line must be byte-identical"
    settings = config.load()
    assert settings.user_read is True
    assert settings.cap("muse") == 0
    assert settings.supervisor_cap("muse") == 1


def test_cap_adds_the_table_to_a_file_that_does_not_have_one(env, capsys):
    """A file that never named the pool gets one table for it, appended
    after everything already in the file and nothing else moved."""
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        "[collector]\ntick_seconds = 7\n", encoding="utf-8")

    assert cli.main(["cap", "grok", "3"]) == 0
    capsys.readouterr()

    text = paths.config_file().read_text(encoding="utf-8")
    assert text.startswith("[collector]\ntick_seconds = 7\n")
    settings = config.load()
    assert settings.user_read is True
    assert settings.cap("grok") == 3


def test_cap_refuses_a_file_it_cannot_parse_and_writes_nothing(env, capsys):
    """Editing a broken file can only make it broken in a new way, and this
    verb is how the owner stops a swarm: it refuses by name and leaves every
    byte where it was, so the owner fixes the file rather than discovering
    later that the limit never took."""
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    broken = "[pool.muse\ncap = ??\n"
    paths.config_file().write_text(broken, encoding="utf-8")

    assert cli.main(["cap", "muse", "1"]) == 1
    err = capsys.readouterr().err

    assert "does not parse" in err
    assert paths.config_file().read_text(encoding="utf-8") == broken


# --------------------------------------------------------------------------
# The Capacity block
# --------------------------------------------------------------------------


def test_the_capacity_block_reads_held_ceiling_and_waiting(env, capsys):
    """Per pool held/cap, per front one line per allocated role reading
    held/ceiling, and the count waiting on a pool with nothing left to
    give. Built from the ledgers, so it is right on a machine whose
    collector has never ticked — this test never runs one."""
    add_front(env, "alpha")
    assert cli.main(["cap", "muse", "1"]) == 0
    capsys.readouterr()
    capacity.grant(pool="muse", front="alpha", role="muse", job="job-held",
                   session="ses-held0001")
    store.append_ledger(paths.front_jobs_path("alpha"), {
        "id": "job-wait", "task": "first work", "kind": "implement",
        "role": "muse", "state": "queued"})
    assert not paths.observed_path().exists()

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out

    block = out.split("Capacity:\n", 1)[1].splitlines()
    assert "  muse: 1/1 held · 1 waiting" in block
    assert "  alpha muse: 1/2 held" in block
    assert "  alpha opus: 0/1 held" in block


def test_a_pool_with_a_free_slot_shows_nobody_waiting(env, capsys):
    """A queued job on a pool that still has a slot is not waiting for one;
    it waits on the supervisor's order, which the Job queue block already
    shows. Counting it here would report capacity pressure that is not
    there."""
    add_front(env, "alpha")
    assert cli.main(["cap", "muse", "3"]) == 0
    capsys.readouterr()
    capacity.grant(pool="muse", front="alpha", role="muse", job="job-held",
                   session="ses-held0001")
    store.append_ledger(paths.front_jobs_path("alpha"), {
        "id": "job-wait", "task": "first work", "kind": "implement",
        "role": "muse", "state": "queued"})

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out

    assert "  muse: 1/3 held" in out
    assert "waiting" not in out.split("Capacity:\n", 1)[1]


def test_the_capacity_block_tracks_a_real_launch_without_a_collector(
        env, capsys):
    """The launcher's grant shows up on the screen immediately, on a
    machine where nothing has ticked. A block read from observed.json alone
    shows an empty pool while a worker is running in it."""
    add_front(env, "alpha")
    capsys.readouterr()
    launched(env, capsys)

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out

    assert "  fake: 1/- held" in out, "an uncapped pool shows no cap, not zero"
    assert "  alpha muse: 1/2 held" in out


def test_an_empty_state_directory_still_says_so(env, capsys):
    """Nothing granted, no front, no tick: the block collapses to one line
    rather than printing an empty heading, and nothing opens the config to
    say it."""
    assert cli.main(["status"]) == 0

    assert "Capacity: no collector data yet." in capsys.readouterr().out
    assert not paths.config_file().exists()


def test_held_is_derived_from_the_open_grants_not_a_stored_number(env):
    """Held is a fold over the ledger. A grant superseded by its own
    released copy counts nothing, and the count survives a state directory
    the collector has never touched. Counting raw lines reads three here."""
    grant = capacity.grant(pool="fake", front="alpha", role="muse",
                           job="job-1", session="ses-one00001")
    store.append_ledger(paths.slots_path(),
                        dict(grant, released_at="2026-09-08T12:00:00+00:00",
                             released_because="returned"))
    capacity.grant(pool="fake", front="alpha", role="muse", job="job-2",
                   session="ses-two00001")

    assert len(store.read_ledger(paths.slots_path())) == 3
    assert capacity.held_by_pool() == {"fake": 1}
    assert capacity.held_by_front_role() == {("alpha", "muse"): 1}


def test_a_supervisor_launch_holds_no_job_slot(env, capsys):
    """A supervisor is the front's own session, not work inside its
    allocation: the worker capacity checks are all below the line that
    dispatches the supervisor shape, so a front at its ceiling can still be
    given a supervisor."""
    add_front(env, "alpha")
    capsys.readouterr()
    launched(env, capsys)
    launched(env, capsys)
    assert "ceiling 2" in refused(env, capsys)

    rc = cli.main(["launch", "supervisor", "alpha", "--repo", str(env),
                   "--dry-run"])
    out, err = capsys.readouterr()

    assert rc == 0, err
    assert "held" not in err
    assert capacity.held_by_pool() == {"fake": 2}
    assert [grant["role"] for grant in capacity.open_grants()] == \
        ["muse", "muse"]


def test_queued_work_is_counted_against_the_pool_its_role_runs_on(
        env, capsys):
    """An `astra` reviewer runs on `codex`. Mapping a role to a pool by
    returning the role unchanged counted the queue against a pool named
    `astra`, which nothing can launch: the codex row said nobody was waiting
    while a reviewer had been waiting for its one slot all along."""
    add_front(env, "alpha")
    assert cli.main(["cap", "codex", "1"]) == 0
    capsys.readouterr()
    capacity.grant(pool="codex", front="alpha", role="astra", job="job-held",
                   session="ses-held0001")
    store.append_ledger(paths.front_jobs_path("alpha"), {
        "id": "job-wait", "task": "first work", "kind": "review",
        "role": "astra", "state": "queued"})

    assert capacity.waiting_by_pool() == {"codex": 1}
    assert cli.main(["status"]) == 0

    block = capsys.readouterr().out.split("Capacity:\n", 1)[1]
    assert "  codex: 1/1 held · 1 waiting" in block
