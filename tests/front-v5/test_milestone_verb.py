"""milestone add appends a ledger line; split/merge/list fold it back.

Every test drives the real CLI against a fresh FOREMAN_STATE with a
v5-shape front whose repository is named ``foreman``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, mcp as mcp_module, paths, store
from foreman.caller import SESSION_ENV

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
OTHER_SUP = "ses-sup0002"
WORKER = "ses-wrk0001"

V5_BRIEF = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
]
supervisor = "grok-4.6:high"
team = [
  "grok-4.6:high:1:supervisor",
  "grok-4.6:high:1:builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5"
target = "main"
check = "python -m pytest tests -q"
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


def iso(moment: datetime) -> str:
    return moment.isoformat()


def make_bare(root: Path) -> tuple[Path, str]:
    src = root / "src-repo"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    bare = root / "remote.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src), str(bare)],
                   check=True)
    return bare, sha


def write_v5(root: Path, name: str, url: str) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=url), encoding="utf-8")
    return brief_dir


def add_front(env: Path, monkeypatch, capsys, name: str = "v5shape") -> str:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    bare, sha = make_bare(env)
    assert cli.main(["front", "add", str(write_v5(env, name, str(bare)))]) == 0
    capsys.readouterr()
    return sha


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def session_entry(sid, role, front):
    return entities.Session.from_dict({
        "id": sid, "role": role, "pool": "opus", "model": "opus",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": iso(NOW - timedelta(minutes=30)),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }).to_dict()


def seed_roster(*entries):
    store.write_snapshot(
        paths.roster_path(),
        {"sessions": {entry["id"]: entry for entry in entries}})


def seed_supervisor(front, sid=SUP):
    seed_roster(session_entry(sid, "supervisor", front))


def add_argv(front="v5shape", title="Front inputs",
             done_when="the front takes the owner's inputs",
             verify="python -m pytest tests -q",
             reason="every later milestone writes onto the front record",
             break_="drop the goal key from a v5 brief", **flags):
    argv = ["milestone", "add", front, "--title", title,
            "--done-when", done_when, "--verify", verify,
            "--reason", reason, "--break", break_]
    if flags.get("order") is not None:
        argv.extend(["--order", str(flags["order"])])
    if flags.get("force"):
        argv.append("--force")
    return argv


def test_milestone_add_appends_every_field(env, monkeypatch, capsys):
    """The supervisor's add lands on milestones.jsonl with every field set."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(order=2), SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    mid = out.strip()
    assert mid.startswith("mil-")
    lines = store.read_ledger(paths.front_milestones_path("v5shape"))
    assert len(lines) == 1
    line = lines[0]
    for key in ("id", "front", "op", "order", "title", "done_when", "verify",
                "reason", "break", "from_ids", "at", "by"):
        assert key in line, key
    assert line["id"] == mid
    assert line["front"] == "v5shape"
    assert line["op"] == "add"
    assert line["order"] == 2
    assert line["title"] == "Front inputs"
    assert line["done_when"] == "the front takes the owner's inputs"
    assert line["verify"] == "python -m pytest tests -q"
    assert line["reason"] == "every later milestone writes onto the front record"
    assert line["break"] == "drop the goal key from a v5 brief"
    assert line["from_ids"] == []
    assert line["by"] == SUP
    assert line["at"]


def test_milestone_add_duplicate_title_is_refused(env, monkeypatch, capsys):
    """A second add with the same title is refused naming title."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(), SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch, add_argv(), SUP) == 1
    _, err = capsys.readouterr()
    assert "title" in err
    assert "Front inputs" in err
    assert len(store.read_ledger(paths.front_milestones_path("v5shape"))) == 1


def test_milestone_add_ninth_is_refused_unless_force(
        env, monkeypatch, capsys):
    """A ninth live milestone is refused unless --force --reason."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    for i in range(8):
        assert run(monkeypatch, add_argv(title=f"Milestone {i}"), SUP) == 0
        capsys.readouterr()
    assert run(monkeypatch, add_argv(title="Milestone 8"), SUP) == 1
    _, err = capsys.readouterr()
    assert "eight" in err
    assert len(store.read_ledger(paths.front_milestones_path("v5shape"))) == 8
    assert run(monkeypatch, add_argv(title="Milestone 8", force=True,
                                     reason="the front needs a ninth"),
               SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    ninth = out.strip()
    lines = store.read_ledger(paths.front_milestones_path("v5shape"))
    assert len(lines) == 9
    assert lines[-1]["id"] == ninth
    assert lines[-1]["force"] is True
    assert lines[-1]["reason"] == "the front needs a ninth"


def test_milestone_add_other_supervisor_is_refused(
        env, monkeypatch, capsys):
    """A supervisor of another front is refused by name."""
    add_front(env, monkeypatch, capsys)
    seed_roster(session_entry(OTHER_SUP, "supervisor", "elsewhere"))
    assert run(monkeypatch, add_argv(), OTHER_SUP) == 1
    _, err = capsys.readouterr()
    assert "elsewhere" in err
    assert "v5shape" in err
    assert store.read_ledger(paths.front_milestones_path("v5shape")) == []


def test_milestone_add_tool_is_listed_for_supervisor_not_worker(
        env, monkeypatch):
    """milestone_add is a supervisor tool and is hidden from a worker."""
    seed_roster(
        session_entry(SUP, "supervisor", "v5shape"),
        session_entry(WORKER, "muse", "v5shape"),
    )
    monkeypatch.setenv(SESSION_ENV, SUP)
    supervisor = {tool["name"] for tool in
                  mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list",
                                     "params": {}})["result"]["tools"]}
    assert "milestone_add" in supervisor
    monkeypatch.setenv(SESSION_ENV, WORKER)
    worker = {tool["name"] for tool in
              mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list",
                                 "params": {}})["result"]["tools"]}
    assert "milestone_add" not in worker


def test_milestone_add_unknown_front_and_empty_fields_are_refused(
        env, monkeypatch, capsys):
    """Unknown front and empty title, done-when, verify, reason, break."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    argv = ["milestone", "add", "missing", "--title", "",
            "--done-when", "", "--verify", "", "--reason", "", "--break", ""]
    assert run(monkeypatch, argv, SUP) == 1
    _, err = capsys.readouterr()
    assert "missing" in err
    assert "--title" in err
    assert "--done-when" in err
    assert "--verify" in err
    assert "--reason" in err
    assert "--break" in err
    assert store.read_ledger(paths.front_milestones_path("v5shape")) == []


def _add_one(monkeypatch, capsys, title="Front inputs"):
    assert run(monkeypatch, add_argv(title=title), SUP) == 0
    return capsys.readouterr().out.strip()


def _split_argv(source, into_titles, reason="split for size"):
    argv = ["milestone", "split", "v5shape", source, "--reason", reason]
    for title in into_titles:
        argv.extend(["--into", title])
        argv.extend([
            "--part",
            f"{title}|done when {title}|python -m pytest tests -q|"
            f"break {title}",
        ])
    return argv


def test_milestone_split_into_two_then_merge_keeps_every_line(
        env, monkeypatch, capsys):
    """Split of one into two leaves three lines, two live, change count 1.

    Merge of those two into one shows change count 2. The file only grows.
    """
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    source = _add_one(monkeypatch, capsys)
    assert run(monkeypatch, _split_argv(source, ["Part A", "Part B"]),
               SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    new_ids = out.strip().split()
    assert len(new_ids) == 2
    assert all(item.startswith("mil-") for item in new_ids)
    raw = store.read_ledger(paths.front_milestones_path("v5shape"))
    assert len(raw) == 3
    from foreman.milestone import _fold, _live, _change_count
    folded = _fold(raw)
    assert len(folded) == 3
    live = _live(folded)
    assert len(live) == 2
    by_id = {item["id"]: item for item in folded}
    assert by_id[source]["split_into"] == new_ids
    assert {item["id"] for item in live} == set(new_ids)
    assert _change_count(raw) == 1
    assert run(monkeypatch, ["milestone", "list", "v5shape"], SUP) == 0
    listed, err = capsys.readouterr()
    assert err == ""
    assert "change count 1" in listed
    assert "Part A" in listed
    assert "Part B" in listed
    assert "pieces done -" in listed
    assert run(monkeypatch, [
        "milestone", "merge", "v5shape", new_ids[0], new_ids[1],
        "--title", "Parts together",
        "--done-when", "both parts are one again",
        "--verify", "python -m pytest tests -q",
        "--break", "drop the merge",
        "--reason", "the split was too fine",
    ], SUP) == 0
    merged, err = capsys.readouterr()
    assert err == ""
    merged_id = merged.strip()
    assert merged_id.startswith("mil-")
    raw_after = store.read_ledger(paths.front_milestones_path("v5shape"))
    assert len(raw_after) == 4
    assert _change_count(raw_after) == 2
    folded_after = _fold(raw_after)
    live_after = _live(folded_after)
    assert len(live_after) == 1
    assert live_after[0]["id"] == merged_id
    by_id_after = {item["id"]: item for item in folded_after}
    assert by_id_after[new_ids[0]]["merged_into"] == [merged_id]
    assert by_id_after[new_ids[1]]["merged_into"] == [merged_id]
    assert run(monkeypatch, ["milestone", "list", "v5shape"], SUP) == 0
    listed, err = capsys.readouterr()
    assert err == ""
    assert "change count 2" in listed
    assert "Parts together" in listed
    assert run(monkeypatch,
               ["milestone", "list", "v5shape", "--history"], SUP) == 0
    hist, err = capsys.readouterr()
    assert err == ""
    assert "history:" in hist
    assert source in hist
    assert new_ids[0] in hist
    assert new_ids[1] in hist
    assert merged_id in hist
    assert len(hist.splitlines()) >= 5  # live + change + history: + 4 lines


def test_milestone_list_json_and_pieces_from_the_tree(
        env, monkeypatch, capsys):
    """--json carries change count; pieces done come from the tree ledger."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    source = _add_one(monkeypatch, capsys)
    assert run(monkeypatch, _split_argv(source, ["Part A", "Part B"]),
               SUP) == 0
    new_ids = capsys.readouterr().out.strip().split()
    store.append_ledger(paths.front_tree_path("v5shape"), {
        "id": "tsk-a", "parent": new_ids[0], "kind": "task",
        "state": "landed",
    }, session_id=SUP)
    store.append_ledger(paths.front_tree_path("v5shape"), {
        "id": "tsk-b", "parent": new_ids[0], "kind": "task",
        "state": "ready",
    }, session_id=SUP)
    store.append_ledger(paths.front_tree_path("v5shape"), {
        "id": "job-a", "parent": "tsk-a", "kind": "job",
        "state": "landed",
    }, session_id=SUP)
    assert run(monkeypatch, ["milestone", "list", "v5shape", "--json"],
               SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert payload["change_count"] == 1
    by_id = {item["id"]: item for item in payload["milestones"]}
    assert by_id[new_ids[0]]["pieces_done"] == "1/2"
    assert by_id[new_ids[1]]["pieces_done"] == "0/0"
    assert run(monkeypatch, ["milestone", "list", "v5shape"], SUP) == 0
    text, err = capsys.readouterr()
    assert err == ""
    assert "pieces done 1/2" in text
    assert "pieces done 0/0" in text


def test_milestone_split_other_supervisor_is_refused(
        env, monkeypatch, capsys):
    """A supervisor of another front is refused by name on split too."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    source = _add_one(monkeypatch, capsys)
    seed_roster(session_entry(OTHER_SUP, "supervisor", "elsewhere"))
    before = store.read_ledger(paths.front_milestones_path("v5shape"))
    assert run(monkeypatch, _split_argv(source, ["Part A", "Part B"]),
               OTHER_SUP) == 1
    _, err = capsys.readouterr()
    assert "elsewhere" in err
    assert "v5shape" in err
    assert store.read_ledger(paths.front_milestones_path("v5shape")) == before


def test_milestone_split_merge_list_tools_are_listed_for_supervisor(
        env, monkeypatch):
    """split, merge and list are supervisor tools, hidden from a worker."""
    seed_roster(
        session_entry(SUP, "supervisor", "v5shape"),
        session_entry(WORKER, "muse", "v5shape"),
    )
    monkeypatch.setenv(SESSION_ENV, SUP)
    supervisor = {tool["name"] for tool in
                  mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list",
                                     "params": {}})["result"]["tools"]}
    assert "milestone_split" in supervisor
    assert "milestone_merge" in supervisor
    assert "milestone_list" in supervisor
    monkeypatch.setenv(SESSION_ENV, WORKER)
    worker = {tool["name"] for tool in
              mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list",
                                 "params": {}})["result"]["tools"]}
    assert "milestone_split" not in worker
    assert "milestone_merge" not in worker
    assert "milestone_list" not in worker
