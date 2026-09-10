"""front land queues a script that lands the work branch onto the target.

A world with a bare origin holding main and a front's work branch one
commit ahead. front land queues a front-landing item; one tick pushes
work onto origin's main with the check recorded. land = pr calls the
fake gh and records pr_url while main does not move.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, fronts, landing, paths, store
from foreman.caller import SESSION_ENV
from foreman.collector import tick
from foreman.entities import Session

SPEC_VERIFY = "true"

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"

V5_BRIEF = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Landing is a queued script.",
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
work = "v5work"
target = "main"
check = "{check}"
land = "{land}"
pr-body = "{pr_body}"
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


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def make_bare(root: Path) -> tuple[Path, Path, str]:
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
    return src, bare, sha


def write_v5(root: Path, name: str, url: str, check: str = "true",
             land: str = "push", pr_body: str = "the front") -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name=name, url=url, check=check, land=land,
                        pr_body=pr_body),
        encoding="utf-8")
    return brief_dir


def add_front(env: Path, monkeypatch, capsys, name: str = "v5shape",
              check: str = "true", land: str = "push",
              pr_body: str = "the front") -> tuple[Path, Path, str]:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    src, bare, sha = make_bare(env)
    assert cli.main(["front", "add",
                     str(write_v5(env, name, str(bare), check=check,
                                  land=land, pr_body=pr_body))]) == 0
    capsys.readouterr()
    record = fronts.read_front_record(name)
    work = (record or {}).get("repositories", [{}])[0].get("work") or "v5work"
    subprocess.run(["git", "-C", str(src), "branch", work, "main"], check=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", "-q", "origin",
         f"+refs/heads/{work}:refs/heads/{work}"],
        check=True)
    return src, bare, sha


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def session_entry(sid, role, front):
    return Session.from_dict({
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
            "--verify", flags.get("verify", SPEC_VERIFY),
            "--must-not-touch", flags.get("must_not_touch",
                                          "the live state directory"),
            "--reason", flags.get("reason", "the tree needs this node"),
            "--break", flags.get("break_", "admit a job as a parent"),
            "--repo", flags.get("repo", "foreman")]
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    return argv


def add_ok(monkeypatch, capsys, **flags):
    assert run(monkeypatch, add_argv(**flags), SUP) == 0
    return capsys.readouterr().out.strip()


def folded_tree(front="v5shape"):
    return {line["id"]: line for line in store.fold_by_id(
        store.read_ledger(paths.front_tree_path(front)))}


def open_clone(env: Path, bare: Path) -> Path:
    clone = env / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    git(clone, "config", "user.email", "test@example.invalid")
    git(clone, "config", "user.name", "foreman-test")
    return clone


def origin_sha(bare: Path, branch: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"],
        check=True, capture_output=True, text=True).stdout.strip()


def add_work_commit(clone: Path, work: str = "v5work",
                    filename: str = "feat.txt", body: str = "feat\n") -> str:
    git(clone, "fetch", "-q", "origin", work)
    git(clone, "checkout", "-q", "-B", work, f"origin/{work}")
    (clone / filename).write_text(body, encoding="utf-8")
    git(clone, "add", filename)
    git(clone, "commit", "-qm", "work ahead")
    git(clone, "push", "-q", "origin", work)
    return git(clone, "rev-parse", "HEAD")


def add_origin_main_commit(clone: Path, filename: str = "main.txt",
                           body: str = "main\n") -> str:
    git(clone, "fetch", "-q", "origin")
    git(clone, "checkout", "-q", "-B", "main", "origin/main")
    (clone / filename).write_text(body, encoding="utf-8")
    git(clone, "add", filename)
    git(clone, "commit", "-qm", "target moved")
    git(clone, "push", "-q", "origin", "main")
    return git(clone, "rev-parse", "HEAD")


def queue_front_land(monkeypatch, capsys, front: str = "v5shape",
                     repo: str | None = None) -> str:
    argv = ["front", "land", front]
    if repo is not None:
        argv.extend(["--repo", repo])
    capsys.readouterr()
    assert run(monkeypatch, argv, None) == 0
    return capsys.readouterr().out.strip()


def scratch_left() -> list[str]:
    root = paths.state_dir() / "worktrees" / "scratch"
    if not root.exists():
        return []
    return [entry.name for entry in root.iterdir()]


def test_front_land_queues_a_script_item_under_the_last_milestone(
        env, monkeypatch, capsys):
    """Owner front land queues a front-landing item under the last milestone."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    mil = add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    capsys.readouterr()
    assert run(monkeypatch, ["front", "land", "v5shape"], None) == 0
    out, err = capsys.readouterr()
    assert err == ""
    item_id = out.strip()
    assert item_id
    item = folded_tree()[item_id]
    assert item["kind"] == "front-landing"
    assert item["role"] == "script"
    assert item["lands"] == "v5work"
    assert item["target"] == "main"
    assert item["parent"] == mil
    assert item["state"] == "queued"
    assert item["repo"] == "foreman"


def test_front_land_refuses_when_a_front_landing_item_is_already_open(
        env, monkeypatch, capsys):
    """A second front land is refused while the first item is still open."""
    add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    first = queue_front_land(monkeypatch, capsys)
    before = store.read_ledger(paths.front_tree_path("v5shape"))
    assert run(monkeypatch, ["front", "land", "v5shape"], None) == 1
    _out, err = capsys.readouterr()
    assert "already open" in err
    assert first in err
    assert store.read_ledger(paths.front_tree_path("v5shape")) == before


def test_one_tick_pushes_work_onto_origin_main(env, monkeypatch, capsys):
    """front land then one tick: origin main moves, the check is recorded."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    clone = open_clone(env, bare)
    work_sha = add_work_commit(clone)
    item_id = queue_front_land(monkeypatch, capsys)
    before_main = origin_sha(bare, "main")
    assert before_main != work_sha
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    after_main = origin_sha(bare, "main")
    assert after_main != before_main
    item = folded_tree()[item_id]
    assert item["state"] == "landed"
    assert item["command"] == "true"
    assert item["exit"] == 0
    assert item["seconds"] is not None and item["seconds"] >= 0
    assert item["output_file"]
    assert Path(item["output_file"]).is_file()
    assert str(paths.state_dir()) in item["output_file"]
    assert item["head"] == after_main
    assert after_main == item["head"]
    tree = subprocess.run(
        ["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only",
         "refs/heads/main"],
        check=True, capture_output=True, text=True).stdout
    assert "feat.txt" in tree
    assert scratch_left() == []


def test_land_pr_records_pr_url_and_does_not_move_main(
        env, monkeypatch, capsys):
    """land = pr: fake gh is called with base, head and body; main stays."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys, land="pr",
                                 pr_body="please land this front")
    seed_supervisor("v5shape")
    add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    clone = open_clone(env, bare)
    add_work_commit(clone)
    calls: list[dict] = []

    def fake_create_pr(*, base, head, title, body, cwd):
        calls.append({"base": base, "head": head, "title": title,
                      "body": body, "cwd": cwd})
        return subprocess.CompletedProcess(
            args=["gh", "pr", "create"], returncode=0,
            stdout="https://example.invalid/pr/7\n", stderr="")

    monkeypatch.setattr(landing, "create_pr", fake_create_pr)
    item_id = queue_front_land(monkeypatch, capsys)
    before_main = origin_sha(bare, "main")
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    assert origin_sha(bare, "main") == before_main
    assert calls
    assert calls[0]["base"] == "main"
    assert calls[0]["head"] == "v5work"
    assert calls[0]["body"] == "please land this front"
    assert calls[0]["title"] == (
        "Foreman starts a front from the owner's inputs alone.")
    item = folded_tree()[item_id]
    assert item["state"] == "landed"
    assert item["pr_url"] == "https://example.invalid/pr/7"
    assert item["command"] == "true"
    assert item["exit"] == 0
    assert scratch_left() == []


def rebase_items(front: str = "v5shape") -> list[dict]:
    return [node for node in folded_tree(front).values()
            if node.get("kind") == "rebase"]


def test_a_moved_target_queues_exactly_one_rebase_item(
        env, monkeypatch, capsys):
    """A commit on origin main behind the front queues one rebase item."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    clone = open_clone(env, bare)
    add_work_commit(clone)
    queue_front_land(monkeypatch, capsys)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    moved = add_origin_main_commit(clone)
    tick(now=NOW + timedelta(seconds=2))
    items = rebase_items()
    assert len(items) == 1
    item = items[0]
    assert item["state"] == "queued"
    assert item["role"] == "script"
    assert item["lands"] == "v5work"
    assert item["onto"] == moved
    record = fronts.read_front_record("v5shape")
    assert record is not None
    assert record.get("behind") == moved
    tick(now=NOW + timedelta(seconds=4))
    assert len(rebase_items()) == 1


def test_rebase_item_moves_work_and_a_second_tick_queues_nothing(
        env, monkeypatch, capsys):
    """Running the rebase puts the moved commit on work and clears behind."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    clone = open_clone(env, bare)
    add_work_commit(clone)
    queue_front_land(monkeypatch, capsys)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    moved = add_origin_main_commit(clone)
    tick(now=NOW + timedelta(seconds=2))
    items = rebase_items()
    assert len(items) == 1
    item = items[0]
    result = landing.run("v5shape", item, by=SUP)
    assert result.ok, result.fail_reason
    work_tree = subprocess.run(
        ["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only",
         "refs/heads/v5work"],
        check=True, capture_output=True, text=True).stdout
    assert "main.txt" in work_tree
    assert "feat.txt" in work_tree
    record = fronts.read_front_record("v5shape")
    assert record is not None
    assert record.get("behind") in (None, "")
    assert record.get("base_sha") == moved
    repos = record.get("repositories") or []
    assert repos and repos[0].get("base_sha") == moved
    recorded = folded_tree()[item["id"]]
    assert recorded["state"] == "landed"
    assert recorded["base_sha"] == moved
    tick(now=NOW + timedelta(seconds=4))
    assert len(rebase_items()) == 1
    assert scratch_left() == []


def test_front_done_is_refused_while_behind_naming_main_and_the_sha(
        env, monkeypatch, capsys):
    """front done names the target and sha while a rebase item is queued."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    clone = open_clone(env, bare)
    add_work_commit(clone)
    queue_front_land(monkeypatch, capsys)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    moved = add_origin_main_commit(clone)
    tick(now=NOW + timedelta(seconds=2))
    items = rebase_items()
    assert len(items) == 1
    assert run(monkeypatch, ["front", "done", "v5shape"], SUP) == 1
    _out, err = capsys.readouterr()
    assert "main" in err
    assert moved in err
    assert items[0]["id"] in err
    assert "rebase" in err
    assert "queued" in err
    record = fronts.read_front_record("v5shape")
    assert record is not None and record.get("state") != "done"


def test_front_done_succeeds_after_the_rebase_item_runs(
        env, monkeypatch, capsys):
    """Once work contains the moved commit and behind is cleared, done lands."""
    _src, bare, _sha = add_front(env, monkeypatch, capsys)
    seed_supervisor("v5shape")
    add_ok(monkeypatch, capsys, title="milestone one", node_id="mil-1")
    clone = open_clone(env, bare)
    add_work_commit(clone)
    queue_front_land(monkeypatch, capsys)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    tick(now=NOW)
    add_origin_main_commit(clone)
    tick(now=NOW + timedelta(seconds=2))
    item = rebase_items()[0]
    result = landing.run("v5shape", item, by=SUP)
    assert result.ok, result.fail_reason
    tick(now=NOW + timedelta(seconds=4))
    assert len(rebase_items()) == 1
    capsys.readouterr()
    assert run(monkeypatch, ["front", "done", "v5shape"], SUP) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.strip() == "v5shape done"
    record = fronts.read_front_record("v5shape")
    assert record is not None and record.get("state") == "done"
