"""A queued node's page is rendered from the node, the map and the sheet.

Every test drives the real CLI (or ``launch.role_sheet`` /
``launch.render_node_page``) against a fresh FOREMAN_STATE with a
v5-shape front whose repository is named ``foreman``.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, entities, launch, paths, store
from foreman.caller import SESSION_ENV

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"

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
    if flags.get("role") is not None:
        argv.extend(["--role", flags["role"]])
    if flags.get("what") is not None:
        argv.extend(["--what", flags["what"]])
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    return argv


def add_ok(monkeypatch, capsys, **flags):
    assert run(monkeypatch, add_argv(**flags), SUP) == 0
    return capsys.readouterr().out.strip()


def add_chain(monkeypatch, capsys, job_id="job-a", role="builder",
              what="implement the door"):
    mil = add_ok(monkeypatch, capsys, title="milestone one",
                 node_id="mil-1")
    tsk = add_ok(monkeypatch, capsys, parent=mil, kind="task",
                 title="the door", node_id="tsk-1")
    job = add_ok(monkeypatch, capsys, parent=tsk, kind="job",
                 title="implement the door", role=role, node_id=job_id,
                 what=what)
    return mil, tsk, job


def folded_tree(front="v5shape"):
    return {line["id"]: line for line in store.fold_by_id(
        store.read_ledger(paths.front_tree_path(front)))}


def test_builder_sheet_is_second_person_and_names_the_rules():
    """The runtime's builder sheet says how the role works."""
    text = launch.role_sheet("builder", {})
    assert text.startswith("You are the builder.")
    assert "Commit as you go" in text
    assert "would go red without the work" in text
    assert "No path under any user's home directory" in text
    assert "exit" in text.lower()


def test_sheet_add_appears_under_the_supervisor_heading():
    """Whoever queues may add to the default; the add sits under its heading."""
    text = launch.role_sheet("builder", {"sheet_add": "read the map first"})
    assert "You are the builder." in text
    assert "## Added by the supervisor" in text
    heading_at = text.index("## Added by the supervisor")
    assert "read the map first" in text[heading_at:]
    assert text.index("You are the builder.") < heading_at


def test_sheet_replace_without_a_reason_is_refused():
    """A full replacement with no recorded reason names the field."""
    with pytest.raises(launch.Refused) as caught:
        launch.role_sheet("builder", {"sheet_replace": "do something else"})
    assert "sheet_reason" in str(caught.value)


def test_sheet_replace_with_a_reason_replaces_the_default():
    """With a reason, the replacement is the whole sheet; the default is gone."""
    text = launch.role_sheet("builder", {
        "sheet_replace": "You are a specialist. Exit when done.",
        "sheet_reason": "this node is not a default builder job",
        "sheet_add": "this add must not appear",
    })
    assert text.strip() == "You are a specialist. Exit when done."
    assert "You are the builder." not in text
    assert "Added by the supervisor" not in text
    assert "this add must not appear" not in text


def test_node_revise_sheet_replace_without_reason_is_refused(
        env, monkeypatch, capsys):
    """--sheet-replace without --sheet-reason names the flag and writes nothing."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, job = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    before = store.read_ledger(paths.front_tree_path("v5shape"))
    assert run(monkeypatch, [
        "node", "revise", "v5shape", job,
        "--sheet-replace", "You are a specialist.",
        "--reason", "try a replacement",
    ], SUP) == 1
    _, err = capsys.readouterr()
    assert "sheet-reason" in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == before


def test_node_revise_sheet_replace_with_reason_writes_both(
        env, monkeypatch, capsys):
    """--sheet-replace TEXT --sheet-reason TEXT stores both on the fold."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    _mil, _tsk, job = add_chain(monkeypatch, capsys)
    capsys.readouterr()
    assert run(monkeypatch, [
        "node", "revise", "v5shape", job,
        "--sheet-replace", "You are a specialist. Exit when done.",
        "--sheet-reason", "this node is not a default builder job",
        "--reason", "replace the sheet",
    ], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == job
    node = folded_tree()[job]
    assert node["sheet_replace"] == "You are a specialist. Exit when done."
    assert node["sheet_reason"] == "this node is not a default builder job"
    sheet = launch.role_sheet(node["role"], node)
    assert "You are the builder." not in sheet
    assert "You are a specialist." in sheet
