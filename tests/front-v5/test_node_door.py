"""node add refuses each named field; node revise is append-only.

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


def add_argv(front="v5shape", parent="v5shape", kind="milestone",
             title="Front inputs", **flags):
    argv = ["node", "add", front, "--parent", parent, "--kind", kind,
            "--title", title,
            "--verify", flags.get("verify", "python -m pytest tests -q"),
            "--must-not-touch", flags.get("must_not_touch",
                                          "the live state directory"),
            "--reason", flags.get("reason", "the tree needs this node"),
            "--break", flags.get("break_", "admit a job as a parent"),
            "--repo", flags.get("repo", "foreman")]
    if flags.get("what") is not None:
        argv.extend(["--what", flags["what"]])
    if flags.get("property") is not None:
        argv.extend(["--property", flags["property"]])
    if flags.get("scope") is not None:
        argv.extend(["--scope", flags["scope"]])
    if flags.get("role") is not None:
        argv.extend(["--role", flags["role"]])
    if flags.get("source") is not None:
        argv.extend(["--source", flags["source"]])
    if flags.get("mechanical"):
        argv.append("--mechanical")
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    for item in flags.get("after") or []:
        argv.extend(["--after", item])
    return argv


def add_ok(monkeypatch, capsys, **flags):
    assert run(monkeypatch, add_argv(**flags), SUP) == 0
    return capsys.readouterr().out.strip()


def test_node_add_appends_every_field(env, monkeypatch, capsys):
    """The supervisor's node lands on tree.jsonl with every field set."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    nid = add_ok(monkeypatch, capsys, title="Front inputs",
                 what="the door refuses a job parent",
                 property="a job parent is accepted",
                 role="builder", node_id="nod-fixed1")
    assert nid == "nod-fixed1"
    lines = store.read_ledger(paths.front_tree_path("v5shape"))
    assert len(lines) == 1
    line = lines[0]
    for key in ("id", "front", "parent", "kind", "title", "repo", "what",
                "verify", "must_not_touch", "reason", "break", "property",
                "scope", "role", "after", "source", "mechanical", "at", "by"):
        assert key in line, key
    assert line["id"] == nid
    assert line["front"] == "v5shape"
    assert line["parent"] == "v5shape"
    assert line["kind"] == "milestone"
    assert line["title"] == "Front inputs"
    assert line["repo"] == "foreman"
    assert line["what"] == "the door refuses a job parent"
    assert line["verify"] == "python -m pytest tests -q"
    assert line["must_not_touch"] == "the live state directory"
    assert line["reason"] == "the tree needs this node"
    assert line["break"] == "admit a job as a parent"
    assert line["property"] == "a job parent is accepted"
    assert line["scope"] == "source-test"
    assert line["role"] == "builder"
    assert line["after"] == []
    assert line["source"] == ""
    assert line["mechanical"] is False
    assert line["by"] == SUP
    assert line["at"]


def test_node_add_milestone_task_job_chain_is_accepted(
        env, monkeypatch, capsys):
    """A milestone, a task under it and a job under the task all write."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys, title="milestone one")
    assert mil.startswith("nod-")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                 title="implement the door")
    lines = store.read_ledger(paths.front_tree_path("v5shape"))
    assert [line["id"] for line in lines] == [mil, tsk, job]
    assert [line["kind"] for line in lines] == ["milestone", "task", "job"]
    assert lines[1]["parent"] == mil
    assert lines[2]["parent"] == tsk


def test_node_add_other_supervisor_is_refused(env, monkeypatch, capsys):
    """A supervisor of another front is refused by name."""
    add_front(env, monkeypatch, capsys)
    seed_roster(session_entry(OTHER_SUP, "supervisor", "elsewhere"))
    assert run(monkeypatch, add_argv(), OTHER_SUP) == 1
    _, err = capsys.readouterr()
    assert "elsewhere" in err
    assert "v5shape" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == []


def test_node_add_parent_not_on_this_front_is_refused(
        env, monkeypatch, capsys):
    """A task whose parent is not on this front's fold names parent."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(parent="nod-missing", kind="task",
                                     title="orphan"), SUP) == 1
    _, err = capsys.readouterr()
    assert "parent" in err
    assert "v5shape" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == []


def test_node_add_milestone_parent_must_equal_the_front(
        env, monkeypatch, capsys):
    """A milestone whose parent is not the front name names parent."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys)
    assert run(monkeypatch, add_argv(parent=mil, kind="milestone",
                                     title="nested"), SUP) == 1
    _, err = capsys.readouterr()
    assert "parent" in err
    assert len(store.read_ledger(paths.front_tree_path("v5shape"))) == 1


def test_node_add_parent_of_kind_derived_is_refused(
        env, monkeypatch, capsys):
    """A child of a derived node names parent; depth 3 is still allowed."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys)
    derived = add_ok(monkeypatch, capsys, parent=mil, kind="derived",
                     title="a copy", source="fronts/v5/tree.jsonl")
    assert run(monkeypatch, add_argv(parent=derived, kind="task",
                                     title="under derived"), SUP) == 1
    _, err = capsys.readouterr()
    assert "parent" in err
    assert "derived" in err
    assert "depth" not in err
    assert len(store.read_ledger(paths.front_tree_path("v5shape"))) == 2


def test_node_add_empty_verify_must_not_touch_reason_break_are_refused(
        env, monkeypatch, capsys):
    """Empty verify, must-not-touch, reason and break are all named at once."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    argv = ["node", "add", "v5shape", "--parent", "v5shape",
            "--kind", "milestone", "--title", "Front inputs",
            "--verify", "", "--must-not-touch", "", "--reason", "",
            "--break", "", "--repo", "foreman"]
    assert run(monkeypatch, argv, SUP) == 1
    _, err = capsys.readouterr()
    assert "--verify" in err
    assert "--must-not-touch" in err
    assert "--reason" in err
    assert "--break" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == []


def test_node_add_fourth_level_is_refused_naming_depth(
        env, monkeypatch, capsys):
    """front → milestone → task → task → job names depth, not parent."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys)
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="outer task")
    inner = add_ok(monkeypatch, capsys, parent=tsk, kind="task",
                   title="inner task")
    assert run(monkeypatch, add_argv(parent=inner, kind="job",
                                     title="too deep"), SUP) == 1
    _, err = capsys.readouterr()
    assert "depth" in err
    assert len(store.read_ledger(paths.front_tree_path("v5shape"))) == 3


def test_node_add_unknown_repo_is_refused(env, monkeypatch, capsys):
    """--repo other names the known repositories and writes nothing."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(repo="other"), SUP) == 1
    _, err = capsys.readouterr()
    assert "other" in err
    assert "foreman" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == []


def test_node_add_after_unknown_node_is_refused(env, monkeypatch, capsys):
    """--after naming a node not on the fold names after."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, add_argv(after=["nod-missing"]), SUP) == 1
    _, err = capsys.readouterr()
    assert "after" in err
    assert "nod-missing" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == []


def test_node_add_derived_without_source_is_refused(
        env, monkeypatch, capsys):
    """A derived node with no --source names source."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys)
    assert run(monkeypatch, add_argv(parent=mil, kind="derived",
                                     title="a copy"), SUP) == 1
    _, err = capsys.readouterr()
    assert "source" in err
    assert len(store.read_ledger(paths.front_tree_path("v5shape"))) == 1


def test_node_add_id_already_used_is_refused(env, monkeypatch, capsys):
    """--id that is already on the fold names id."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    first = add_ok(monkeypatch, capsys, node_id="nod-taken1")
    assert first == "nod-taken1"
    assert run(monkeypatch, add_argv(title="second",
                                     node_id="nod-taken1"), SUP) == 1
    _, err = capsys.readouterr()
    assert "id" in err
    assert "nod-taken1" in err
    assert len(store.read_ledger(paths.front_tree_path("v5shape"))) == 1


def test_node_add_tool_is_listed_for_supervisor_not_worker(
        env, monkeypatch):
    """node_add is a supervisor tool and is hidden from a worker session."""
    seed_roster(
        session_entry(SUP, "supervisor", "v5shape"),
        session_entry(WORKER, "muse", "v5shape"),
    )
    monkeypatch.setenv(SESSION_ENV, SUP)
    supervisor = {tool["name"] for tool in
                  mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list",
                                     "params": {}})["result"]["tools"]}
    assert "node_add" in supervisor
    assert "node_revise" in supervisor
    assert "node_list" in supervisor
    monkeypatch.setenv(SESSION_ENV, WORKER)
    worker = {tool["name"] for tool in
              mcp_module.handle({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list",
                                 "params": {}})["result"]["tools"]}
    assert "node_add" not in worker
    assert "node_revise" not in worker
    assert "node_list" not in worker


def test_node_revise_appends_and_the_file_only_grows(
        env, monkeypatch, capsys):
    """A revise of title appends a copy with op=revise; the first line stays."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    nid = add_ok(monkeypatch, capsys, title="Front inputs")
    first = store.read_ledger(paths.front_tree_path("v5shape"))
    assert run(monkeypatch, ["node", "revise", "v5shape", nid,
                             "--title", "Front inputs closed",
                             "--reason", "the map landed"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == nid
    lines = store.read_ledger(paths.front_tree_path("v5shape"))
    assert len(lines) == 2
    assert lines[0] == first[0]
    assert lines[1]["id"] == nid
    assert lines[1]["op"] == "revise"
    assert lines[1]["title"] == "Front inputs closed"
    assert lines[1]["reason_revised"] == "the map landed"
    folded = store.fold_by_id(lines)
    assert len(folded) == 1
    assert folded[0]["title"] == "Front inputs closed"


def test_node_revise_changing_parent_is_refused(env, monkeypatch, capsys):
    """--parent on revise names parent and writes nothing new."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    nid = add_ok(monkeypatch, capsys)
    assert run(monkeypatch, ["node", "revise", "v5shape", nid,
                             "--parent", "elsewhere",
                             "--reason", "move it"], SUP) == 1
    _, err = capsys.readouterr()
    assert "parent" in err
    assert len(store.read_ledger(paths.front_tree_path("v5shape"))) == 1


def test_node_revise_changing_kind_is_refused(env, monkeypatch, capsys):
    """--kind on revise names kind and writes nothing new."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    nid = add_ok(monkeypatch, capsys)
    assert run(monkeypatch, ["node", "revise", "v5shape", nid,
                             "--kind", "task",
                             "--reason", "reclassify"], SUP) == 1
    _, err = capsys.readouterr()
    assert "kind" in err
    assert len(store.read_ledger(paths.front_tree_path("v5shape"))) == 1


def test_node_revise_state_sets_landed(env, monkeypatch, capsys):
    """Until landing lands, --state on revise is what marks a job landed."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys)
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task", title="door")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job", title="build")
    assert run(monkeypatch, ["node", "revise", "v5shape", job,
                             "--state", "landed",
                             "--reason", "until milestone 4"], SUP) == 0
    capsys.readouterr()
    folded = store.fold_by_id(
        store.read_ledger(paths.front_tree_path("v5shape")))
    by_id = {line["id"]: line for line in folded}
    assert by_id[job]["state"] == "landed"
    assert by_id[job]["op"] == "revise"


def test_node_list_prints_the_chain_indented(env, monkeypatch, capsys):
    """A milestone/task/job chain prints indented by depth; tasks show 0/1."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys, title="milestone one")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                 title="implement the door")
    assert run(monkeypatch, ["node", "list", "v5shape"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    lines = out.splitlines()
    assert lines == [
        f"{mil}  milestone  milestone one",
        f"  {tsk}  task  the door  0/1",
        f"    {job}  job  implement the door",
    ]


def test_node_list_empty_prints_no_tree_yet(env, monkeypatch, capsys):
    """A front with no nodes prints (no tree yet)."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, ["node", "list", "v5shape"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == "(no tree yet)"


def test_node_list_json_prints_the_folded_list(env, monkeypatch, capsys):
    """--json prints the folded list, not the text rendering."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys, title="milestone one")
    assert run(monkeypatch, ["node", "list", "v5shape", "--json"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    folded = json.loads(out)
    assert isinstance(folded, list)
    assert len(folded) == 1
    assert folded[0]["id"] == mil
    assert folded[0]["kind"] == "milestone"
    assert folded[0]["title"] == "milestone one"


def test_node_list_under_and_landed_count(env, monkeypatch, capsys):
    """--under a task prints that subtree; a landed job counts 1/1."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys, title="milestone one")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                 title="implement the door")
    other = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                   title="sibling")
    assert run(monkeypatch, ["node", "revise", "v5shape", job,
                             "--state", "landed",
                             "--reason", "until milestone 4"], SUP) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["node", "list", "v5shape", "--under", tsk],
               SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert mil not in out
    assert other not in out
    assert lines_of(out) == [
        f"  {tsk}  task  the door  1/1",
        f"    {job}  job  implement the door",
    ]
    assert run(monkeypatch, ["node", "list", "v5shape"], SUP) == 0
    listed, err = capsys.readouterr()
    assert err == ""
    assert f"  {tsk}  task  the door  1/1" in listed
    assert f"  {other}  task  sibling  0/0" in listed


def test_node_list_unknown_under_is_refused(env, monkeypatch, capsys):
    """--under an id not on the fold names under."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    add_ok(monkeypatch, capsys)
    assert run(monkeypatch, ["node", "list", "v5shape",
                             "--under", "nod-missing"], SUP) == 1
    _, err = capsys.readouterr()
    assert "under" in err
    assert "nod-missing" in err


def test_node_list_unknown_front_is_refused(env, monkeypatch, capsys):
    """Unknown front is refused; an empty existing front is not unknown."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    assert run(monkeypatch, ["node", "list", "missing"], SUP) == 1
    _, err = capsys.readouterr()
    assert "missing" in err


def lines_of(out: str) -> list[str]:
    return out.splitlines()
