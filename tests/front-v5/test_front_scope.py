"""A session sees only its own front; the foreman and the owner see all.

Every test drives the real CLI against a fresh FOREMAN_STATE. Two v5
fronts are written directly so the tests name the sessions they need;
``front add`` is not the seam.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foreman import caller as caller_module
from foreman import capacity, cli, entities, ids, mcp as mcp_module, paths, store
from foreman.caller import FOREMAN, OWNER, SESSION_ENV, SUPERVISOR
from foreman.status import NOW_ENV

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
PINNED_NOW = "2026-09-10T12:00:00+00:00"
OWN = "v5"
OTHER = "orbit"
SUP = "ses-sup-v5"
OTHER_SUP = "ses-sup-orb"
FOREMAN_SID = "ses-foreman"
WORKER = "ses-wrk-v5"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.setattr(capacity, "_snapshot", lambda: {})
    return tmp_path


def write_v5(name: str, state: str = "active") -> dict:
    paths.front_dir(name).mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "grok-4.6", "pool": "grok", "model": "grok-4.6",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "grok-4.6", "pool": "grok", "model": "grok-4.6",
         "effort": "high", "count": 1, "role": "builder"},
    ]
    line = entities.Front(
        id=ids.mint("front"), name=name, state=state, shape="v5",
        goal="Show the caller's own front.",
        finish_line="A session sees only its front.",
        allocation={"grok": 1}, team=team,
        repositories=[{"name": "foreman", "target": "main",
                       "work": name, "base": "main",
                       "url": "https://example.invalid/foreman.git"}],
    ).to_dict()
    store.append_ledger(paths.front_record_path(name), line)
    return line


def session_entry(sid: str, role: str, front: str | None) -> dict:
    return entities.Session.from_dict({
        "id": sid, "role": role, "pool": "grok", "model": "grok-4.6",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": PINNED_NOW,
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }).to_dict()


def seed_roster(*entries: dict) -> None:
    store.write_snapshot(
        paths.roster_path(),
        {"sessions": {entry["id"]: entry for entry in entries}})


def two_fronts() -> None:
    write_v5(OWN, state="queued")
    write_v5(OTHER, state="queued")
    seed_roster(
        session_entry(SUP, "supervisor", OWN),
        session_entry(OTHER_SUP, "supervisor", OTHER),
        session_entry(FOREMAN_SID, "foreman", None),
        session_entry(WORKER, "muse", OWN),
    )


def run(monkeypatch, argv, session=None) -> int:
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def refused(monkeypatch, capsys, argv, session: str) -> str:
    assert run(monkeypatch, argv, session) == 1
    out, err = capsys.readouterr()
    assert out == ""
    return err


def test_visible_fronts_owner_and_foreman_see_all():
    """None means every front."""
    owner = caller_module.Caller(role=OWNER, session_id=None, session={})
    foreman = caller_module.Caller(
        role=FOREMAN, session_id=FOREMAN_SID, session={})
    assert caller_module.visible_fronts(owner) is None
    assert caller_module.visible_fronts(foreman) is None


def test_visible_fronts_supervisor_and_worker_see_their_front():
    """The roster front, as a one-item list."""
    supervisor = caller_module.Caller(
        role=SUPERVISOR, session_id=SUP, session={"front": OWN})
    worker = caller_module.Caller(
        role="muse", session_id=WORKER, session={"front": OWN})
    assert caller_module.visible_fronts(supervisor) == [OWN]
    assert caller_module.visible_fronts(worker) == [OWN]


def test_visible_fronts_unknown_session_sees_none():
    assert caller_module.visible_fronts(None) == []


def test_supervisor_status_names_only_its_front(env, monkeypatch, capsys):
    """Working, queue, needs-you and problems hide the other front."""
    two_fronts()
    store.append_ledger(
        paths.inbox_path(),
        entities.InboxItem(
            id="inb-orbit1", from_=OTHER_SUP, kind="scope",
            question="Widen orbit to cover the harbor light?",
            recommendation="No.", asked_at=PINNED_NOW,
        ).to_dict())
    store.append_ledger(
        paths.anomalies_path(),
        entities.Anomaly(
            kind="supervisor silent", subject=OTHER_SUP,
            since=PINNED_NOW, detail="orbit wrote nothing",
        ).to_dict())
    assert run(monkeypatch, ["status"], SUP) == 0
    out = capsys.readouterr().out
    before_cap = out.split("Capacity:", 1)[0]
    assert OWN in before_cap
    assert OTHER not in before_cap
    assert "Widen orbit" not in out
    assert "orbit wrote nothing" not in out


def test_supervisor_is_refused_reads_of_another_front(
        env, monkeypatch, capsys):
    """Each named read verb refuses the foreign front, naming both."""
    two_fronts()
    expected = f"front '{OTHER}' is not yours ({OWN})"
    verbs = (
        ["map", "show", OTHER],
        ["node", "list", OTHER],
        ["milestone", "list", OTHER],
        ["job", "list", OTHER],
        ["front", "show", OTHER],
        ["front", "policy", OTHER, "foreman"],
        ["evidence", "list", OTHER],
    )
    for argv in verbs:
        err = refused(monkeypatch, capsys, argv, SUP)
        assert expected in err, argv
        assert OWN in err and OTHER in err


def test_front_queue_prints_both_names_for_a_supervisor(
        env, monkeypatch, capsys):
    """The queue order is public; its contents are not."""
    two_fronts()
    assert run(monkeypatch, ["front", "queue"], SUP) == 0
    out = capsys.readouterr().out
    assert OWN in out
    assert OTHER in out


def test_front_list_prints_both_names_for_a_supervisor(
        env, monkeypatch, capsys):
    two_fronts()
    assert run(monkeypatch, ["front", "list"], SUP) == 0
    out = capsys.readouterr().out
    assert OWN in out
    assert OTHER in out


def test_foreman_and_owner_see_both_fronts(env, monkeypatch, capsys):
    """The foreman session and the owner read every front."""
    two_fronts()
    for session in (FOREMAN_SID, None):
        assert run(monkeypatch, ["status"], session) == 0
        out = capsys.readouterr().out
        assert OWN in out and OTHER in out
        assert run(monkeypatch, ["map", "show", OTHER], session) == 0
        shown = capsys.readouterr().out
        assert "no map yet" in shown or OTHER in shown or shown == ""
        assert run(monkeypatch, ["node", "list", OTHER], session) == 0
        capsys.readouterr()
        assert run(monkeypatch, ["milestone", "list", OTHER], session) == 0
        capsys.readouterr()
        assert run(monkeypatch, ["job", "list", OTHER], session) == 0
        capsys.readouterr()
        assert run(monkeypatch, ["front", "show", OTHER], session) == 0
        shown = capsys.readouterr().out
        assert "state:" in shown
        assert "Show the caller's own front." in shown
        assert run(monkeypatch, ["front", "queue"], session) == 0
        queued = capsys.readouterr().out
        assert OWN in queued and OTHER in queued


def test_worker_status_names_only_its_front(env, monkeypatch, capsys):
    """A worker on A's job sees A only."""
    two_fronts()
    assert run(monkeypatch, ["status"], WORKER) == 0
    out = capsys.readouterr().out
    before_cap = out.split("Capacity:", 1)[0]
    assert OWN in before_cap
    assert OTHER not in before_cap


def test_status_capacity_stays_whole_for_a_supervisor(
        env, monkeypatch, capsys):
    """Pool numbers are public: Capacity is not filtered."""
    two_fronts()
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["front", "reserve", OTHER]) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["status"], None) == 0
    owner = capsys.readouterr().out
    assert run(monkeypatch, ["status"], SUP) == 0
    scoped = capsys.readouterr().out
    owner_cap = owner[owner.index("Capacity:"):owner.index("Overall:")]
    scoped_cap = scoped[scoped.index("Capacity:"):scoped.index("Overall:")]
    assert owner_cap == scoped_cap
    assert OTHER not in scoped.split("Capacity:")[0]


def test_resource_list_hides_another_fronts_holders(
        env, monkeypatch, capsys):
    """Holders on a foreign front are omitted; the counts stay."""
    two_fronts()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "foreman.toml").write_text(
        "[resources]\nfixture-db = 2\n", encoding="utf-8")
    store.append_ledger(paths.front_tree_path(OWN), {
        "id": "job-own", "front": OWN, "kind": "job",
        "state": "running", "resources": ["fixture-db"],
    })
    store.append_ledger(paths.front_tree_path(OTHER), {
        "id": "job-orb", "front": OTHER, "kind": "job",
        "state": "running", "resources": ["fixture-db"],
    })
    assert run(monkeypatch, ["resource", "list"], SUP) == 0
    out = capsys.readouterr().out
    assert "fixture-db: held 2 / count 2" in out
    assert f"  {OWN} job-own" in out
    assert OTHER not in out
    assert "job-orb" not in out
    assert run(monkeypatch, ["resource", "list"], None) == 0
    owner = capsys.readouterr().out
    assert f"  {OWN} job-own" in owner
    assert f"  {OTHER} job-orb" in owner


def _front_argument(tool: dict) -> dict:
    """Arguments that aim a read tool at the foreign front, or {}."""
    props = tool.get("inputSchema", {}).get("properties", {})
    arguments: dict = {}
    if "action" in props:
        arguments["action"] = "list"
    if "front" in props:
        arguments["front"] = OTHER
    elif "name" in props:
        arguments["name"] = OTHER
    if "repo" in props:
        arguments["repo"] = "foreman"
    return arguments


def test_supervisor_mcp_reads_say_own_front_and_refuse_a_foreign_one(
        env, monkeypatch, capsys):
    """The supervisor row's read verbs name own front and refuse orbit."""
    two_fronts()
    monkeypatch.setenv(SESSION_ENV, SUP)
    tools = mcp_module.tools_for("supervisor")
    stamped = [tool for tool in tools
               if "own front" in (tool.get("description") or "")]
    names = {tool["name"] for tool in stamped}
    assert names == set(mcp_module.OWN_FRONT_READS)
    expected = f"front '{OTHER}' is not yours ({OWN})"
    walked = []
    for tool in stamped:
        arguments = _front_argument(tool)
        if "front" not in arguments and "name" not in arguments:
            continue
        walked.append(tool["name"])
        text, failed = mcp_module.call_tool(tool["name"], arguments)
        assert failed is True, tool["name"]
        assert expected in text, (tool["name"], text)
    assert set(walked) == set(mcp_module.OWN_FRONT_READS) - {
        "resource_list", "status",
    }
