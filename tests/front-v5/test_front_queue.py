"""The front queue: order, why each waits, start, stop, resume.

The world caps a fake pool at 3. Front records are written directly so
the tests name the team they need; ``front add`` is not the seam. Worker
and supervisor spawn is substituted: nothing here reaches systemd.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foreman import cli, entities, fronts, ids, paths, store
from foreman import launch as launch_module
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter
from foreman.pools import _common

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
    monkeypatch.setattr(_common, "stop_unit", lambda *a, **k: False)
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


def open_for(front: str) -> list[dict]:
    return [rec for rec in fronts.open_reservations()
            if rec.get("front") == front]


@pytest.fixture()
def supervisor_spawn(env, monkeypatch):
    """Capture ``launch_supervisor_headless`` and roster a fake session.

    The tick's last door before a unit: production would run a first
    turn under systemd. The captured Namespace is the model and effort
    the record asked for.
    """
    calls: list = []

    def fake(args, *, front, record, repo, branch):
        sid = f"ses-sup{len(calls) + 1:04d}"
        calls.append({"args": args, "front": front, "record": record,
                      "repo": repo, "branch": branch, "session": sid})

        def add(roster):
            if not isinstance(roster, dict):
                roster = {"sessions": {}}
            sessions = roster.setdefault("sessions", {})
            sessions[sid] = Session(
                id=sid, role="supervisor", pool="opus",
                model=getattr(args, "model", None) or "",
                front=front, state="running", headless=True,
            ).to_dict()
            return roster

        store.update_snapshot(paths.roster_path(), add,
                              default={"sessions": {}})
        fronts.set_front_supervisor(front, sid, by="collector")
        print(f"session: {sid}")
        return 0

    monkeypatch.setattr(launch_module, "launch_supervisor_headless", fake)
    return calls


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


def test_one_tick_starts_the_top_front_and_not_the_second(
        env, capsys, supervisor_spawn):
    """Two queued fronts wanting 2 and 2 on a cap of 3: one tick starts
    the top (model, effort, builders reservation, state active) and
    leaves the second queued."""
    write_v5("alpha", builders=2, model="fake-test-model", effort="high")
    write_v5("beta", builders=2)
    tick(now=NOW)
    assert [item["front"] for item in supervisor_spawn] == ["alpha"]
    launched = supervisor_spawn[0]["args"]
    assert launched.model == "fake-test-model"
    assert launched.effort == "high"
    recs = open_for("alpha")
    assert len(recs) == 1
    assert recs[0]["phase"] == "builders"
    assert recs[0]["count"] == 2
    assert recs[0]["pool"] == "fake"
    assert open_for("beta") == []
    alpha = fronts.read_front_record("alpha")
    beta = fronts.read_front_record("beta")
    assert alpha["state"] == "active"
    assert alpha["started_at"] == NOW.isoformat()
    assert beta["state"] == "queued"
    starts = store.read_snapshot(paths.collector_path())["front_starts"]
    assert starts[0]["front"] == "alpha"
    assert starts[0]["session"] == supervisor_spawn[0]["session"]
    assert starts[0]["at"] == NOW.isoformat()


def test_next_tick_leaves_the_second_queued_naming_the_pool(
        env, capsys, supervisor_spawn):
    """After the top has started, the next tick does not start the
    second; front queue names the pool numbers."""
    write_v5("alpha", builders=2)
    write_v5("beta", builders=2)
    tick(now=NOW)
    capsys.readouterr()
    tick(now=NOW + timedelta(seconds=2))
    assert [item["front"] for item in supervisor_spawn] == ["alpha"]
    assert fronts.read_front_record("beta")["state"] == "queued"
    assert cli.main(["front", "queue"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "beta  pool fake: cap 3, reserved 2, wants 2",
    ]


def test_a_second_front_whose_team_fits_never_starts_first(
        env, supervisor_spawn):
    """Both fronts fit on a cap of 3 wanting 1 and 1; the second still
    does not start before the first."""
    write_v5("alpha", builders=1)
    write_v5("beta", builders=1)
    tick(now=NOW)
    assert [item["front"] for item in supervisor_spawn] == ["alpha"]
    assert fronts.read_front_record("alpha")["state"] == "active"
    assert fronts.read_front_record("beta")["state"] == "queued"


def test_a_second_front_that_fits_waits_while_the_top_does_not(
        env, supervisor_spawn):
    """The top front wants more than the cap and never fits; the second
    fits on its own. One tick starts nothing: strict order."""
    write_v5("alpha", builders=4)
    write_v5("beta", builders=1)
    tick(now=NOW)
    assert supervisor_spawn == []
    assert fronts.read_front_record("alpha")["state"] == "queued"
    assert fronts.read_front_record("beta")["state"] == "queued"
    assert open_for("beta") == []


def test_a_refused_launch_releases_and_leaves_the_front_queued(
        env, monkeypatch, capsys):
    """A launch that returns non-zero releases the reservation, stays
    queued, and records start_refused."""
    write_v5("alpha", builders=2)
    write_v5("beta", builders=2)

    def refuse(args, *, front, record, repo, branch):
        print("foreman launch: refused", file=__import__("sys").stderr)
        print("- no window", file=__import__("sys").stderr)
        return 1

    monkeypatch.setattr(launch_module, "launch_supervisor_headless", refuse)
    tick(now=NOW)
    alpha = fronts.read_front_record("alpha")
    assert alpha["state"] == "queued"
    assert alpha.get("start_refused")
    assert open_for("alpha") == []
    assert fronts.read_front_record("beta")["state"] == "queued"
    assert supervisor_spawn_not_called(env)


def supervisor_spawn_not_called(env):
    """No supervisor session was rostered by a refused start."""
    roster = store.read_snapshot(paths.roster_path(), default={"sessions": {}})
    sessions = roster.get("sessions") or {}
    return all(entry.get("role") != "supervisor"
               for entry in sessions.values()
               if isinstance(entry, dict))


def roster_session(sid: str) -> dict | None:
    roster = store.read_snapshot(paths.roster_path(), default={"sessions": {}})
    sessions = roster.get("sessions") or {}
    entry = sessions.get(sid)
    return entry if isinstance(entry, dict) else None


def test_stop_kills_releases_and_the_next_tick_starts_the_second(
        env, capsys, supervisor_spawn):
    """front stop on the active front kills its session, releases the
    reservation, and the next tick starts the second."""
    write_v5("alpha", builders=2)
    write_v5("beta", builders=2)
    store.append_ledger(paths.front_jobs_path("alpha"), {
        "id": "job-queued1", "state": "queued", "role": "muse",
    })
    tick(now=NOW)
    sid = supervisor_spawn[0]["session"]
    capsys.readouterr()
    assert cli.main(["front", "stop", "alpha",
                     "--reason", "owner paused this front"]) == 0
    out = capsys.readouterr().out
    assert "alpha stopped" in out
    assert roster_session(sid)["state"] == "killed"
    assert open_for("alpha") == []
    alpha = fronts.read_front_record("alpha")
    assert alpha["state"] == "stopped"
    assert alpha["stop_reason"] == "owner paused this front"
    jobs = store.fold_by_id(store.read_ledger(paths.front_jobs_path("alpha")))
    assert jobs[0]["state"] == "queued"
    tick(now=NOW + timedelta(seconds=2))
    assert [item["front"] for item in supervisor_spawn] == ["alpha", "beta"]
    assert fronts.read_front_record("beta")["state"] == "active"
    recs = open_for("beta")
    assert len(recs) == 1 and recs[0]["count"] == 2


def test_resume_requeues_the_stopped_front(
        env, capsys, supervisor_spawn):
    """front resume puts a stopped front back in the queue with prefer
    unchanged, so it waits behind the one that started in its place."""
    write_v5("alpha", builders=2, prefer=0)
    write_v5("beta", builders=2, prefer=0)
    tick(now=NOW)
    capsys.readouterr()
    assert cli.main(["front", "stop", "alpha",
                     "--reason", "owner paused this front"]) == 0
    capsys.readouterr()
    tick(now=NOW + timedelta(seconds=2))
    capsys.readouterr()
    assert cli.main(["front", "resume", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "alpha queued" in out
    alpha = fronts.read_front_record("alpha")
    assert alpha["state"] == "queued"
    assert alpha["prefer"] == 0
    assert not alpha.get("stop_reason")
    assert cli.main(["front", "queue"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "alpha  pool fake: cap 3, reserved 2, wants 2",
    ]


def test_stop_and_resume_refuse_the_wrong_state(env, capsys, supervisor_spawn):
    """Both verbs name the state the front is actually in."""
    write_v5("alpha", builders=2)
    write_v5("beta", builders=2)
    assert cli.main(["front", "stop", "alpha",
                     "--reason", "too soon"]) == 1
    assert "is queued, not active" in capsys.readouterr().err
    tick(now=NOW)
    capsys.readouterr()
    assert cli.main(["front", "resume", "alpha"]) == 1
    assert "is active, not stopped" in capsys.readouterr().err
    assert cli.main(["front", "stop", "alpha",
                     "--reason", "owner paused this front"]) == 0
    capsys.readouterr()
    assert cli.main(["front", "stop", "alpha",
                     "--reason", "again"]) == 1
    assert "is stopped, not active" in capsys.readouterr().err
    assert cli.main(["front", "resume", "beta"]) == 1
    assert "is queued, not stopped" in capsys.readouterr().err


def test_stop_without_a_reason_is_refused(env, capsys, supervisor_spawn):
    write_v5("alpha", builders=2)
    tick(now=NOW)
    capsys.readouterr()
    assert cli.main(["front", "stop", "alpha"]) == 1
    assert "field '--reason' is required" in capsys.readouterr().err
    assert fronts.read_front_record("alpha")["state"] == "active"
