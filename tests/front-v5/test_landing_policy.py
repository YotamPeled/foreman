"""policy_for reads the repository policy; the global [merge] check is a fallback.

A v5 front's matching repository entry wins even when ``[merge] check``
says the opposite. An old-shape front, or a v5 front with no matching
entry, falls back. Neither having a check is a refusal that names the
front and the repository. ``foreman front policy`` prints the fields.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foreman import cli, fronts, landing, paths, store
from foreman.caller import Refusal, SESSION_ENV

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
SUP = "ses-sup0001"
DESK = "ses-desk001"

OLD_BRIEF = '''name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 2

[[task]]
title = "first"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "true"
size = 1
after = []
'''

V5_BRIEF = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
  "Landing is a queued script.",
]
supervisor = "grok-4.6:high"
team = [
  "grok-4.6:high:1:supervisor",
  "grok-4.6:high:2:builder",
  "opus-5:high:1:backup-builder",
  "astra-6:low:1:reviewer",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5"
target = "main"
check = "true"
land = "push"
trailers = ["Signed-off-by: Foreman"]
pr-body = "the front"
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


def make_bare(root: Path) -> Path:
    src = root / "src-repo"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    bare = root / "remote.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src), str(bare)],
                   check=True)
    return bare


def write_merge_fallback(root: Path, command: str) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "foreman.toml").write_text(
        f"[merge]\ncheck = {json.dumps(command)}\n", encoding="utf-8")


def add_v5(root: Path, name: str = "v5shape", text: str | None = None) -> Path:
    bare = make_bare(root)
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    body = V5_BRIEF.format(name=name, url=str(bare)) if text is None else text
    (brief_dir / "brief.toml").write_text(body, encoding="utf-8")
    assert fronts.front_add_main(str(brief_dir)) == 0
    return bare


def add_old(root: Path, name: str = "oldshape") -> None:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(
        OLD_BRIEF.format(name=name), encoding="utf-8")
    assert fronts.front_add_main(str(brief_dir)) == 0


def test_policy_for_v5_repository_beats_merge_fallback(env):
    """A v5 repository check wins over ``[merge] check = "false"``."""
    write_merge_fallback(env, "false")
    add_v5(env)
    policy = landing.policy_for("v5shape", "foreman")
    assert policy.check == "true"
    assert policy.source == "repository foreman"
    assert policy.land == "push"
    assert policy.trailers == ["Signed-off-by: Foreman"]
    assert policy.pr_body == "the front"
    assert policy.base == "main"
    assert policy.work == "v5"
    assert policy.target == "main"


def test_policy_for_old_shape_uses_merge_fallback(env):
    """An old-shape front has no repository entry: the global check answers."""
    write_merge_fallback(env, "false")
    add_old(env)
    policy = landing.policy_for("oldshape", "foreman")
    assert policy.check == "false"
    assert policy.source == "[merge] fallback"
    assert policy.land == "push"
    assert policy.trailers == []


def test_policy_for_refuses_when_neither_has_a_check(env):
    """A v5 repository with no check and no ``[merge]`` table names both."""
    store.append_ledger(paths.front_record_path("nocheck"), {
        "id": "frt-nocheck",
        "name": "nocheck",
        "shape": "v5",
        "repositories": [{
            "name": "foreman",
            "url": "/no/such/remote",
            "base": "main",
            "work": "v5",
            "target": "main",
            "check": "",
            "land": "push",
            "trailers": [],
            "pr_body": "",
        }],
    })
    with pytest.raises(Refusal) as caught:
        landing.policy_for("nocheck", "foreman")
    message = str(caught.value)
    assert "nocheck" in message
    assert "foreman" in message


def test_front_policy_prints_the_fields(env, capsys):
    """``foreman front policy`` prints every Policy field, one per line."""
    write_merge_fallback(env, "false")
    bare = add_v5(env)
    capsys.readouterr()
    rc = cli.main(["front", "policy", "v5shape", "foreman"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert err == ""
    lines = {line.split(":", 1)[0]: line.split(":", 1)[1].lstrip()
             for line in out.strip().splitlines() if ":" in line}
    assert lines["check"] == "true"
    assert lines["land"] == "push"
    assert lines["trailers"] == "Signed-off-by: Foreman"
    assert lines["pr_body"] == "the front"
    assert lines["script"] == ""
    assert lines["base"] == "main"
    assert lines["work"] == "v5"
    assert lines["target"] == "main"
    assert lines["url"] == str(bare)
    assert lines["source"] == "repository foreman"
    assert list(lines) == [
        "check", "land", "trailers", "pr_body", "script",
        "base", "work", "target", "url", "source",
    ]


# --------------------------------------------------------------------------
# merge land reads the policy
# --------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def make_repo(root: Path) -> Path:
    """A repo with a bare origin beside it, both under tmp_path."""
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-qm", "seed")
    origin = root / "origin.git"
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    return repo


def make_branch(repo: Path, name: str, filename: str) -> None:
    git(repo, "checkout", "-qb", name)
    (repo / filename).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", filename)
    git(repo, "commit", "-qm", name)
    git(repo, "checkout", "-q", "main")


def run(monkeypatch, argv, session=None, cwd=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    if cwd is not None:
        monkeypatch.chdir(cwd)
    return cli.main(argv)


def seed_roster(*entries):
    store.write_snapshot(
        paths.roster_path(),
        {"sessions": {entry["id"]: entry for entry in entries}})


def session_record(sid, role, front=None):
    started = (NOW - timedelta(minutes=30)).isoformat()
    return {
        "id": sid, "role": role, "pool": "opus", "model": "opus",
        "front": front, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "20m",
        "launched_by": "owner",
        "started_at": started,
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }


def seed_built_task(front: str, title: str = "first") -> str:
    tid = f"tsk-{front[:7]}1"
    store.append_ledger(paths.front_tasks_path(front), {
        "id": tid, "front": front, "title": title,
        "scope": "WHAT: x.\nINPUTS: y.\nOUTPUTS: z.\nOUT OF SCOPE: n.",
        "verify": "true", "size": 1, "after": [],
        "state": "built", "units_done": 1, "units_total": 1,
    })
    return tid


def request_take_land(monkeypatch, capsys, repo: Path, front: str,
                      task: str) -> tuple[int, str, str]:
    assert run(monkeypatch, ["merge", "request", "feat",
                             "--front", front, "--tasks", task,
                             "--target", "main"], SUP, cwd=repo) == 0
    mid = capsys.readouterr().out.split()[0]
    assert run(monkeypatch, ["merge", "take", mid], DESK) == 0
    capsys.readouterr()
    rc = run(monkeypatch, ["merge", "land", mid], DESK, cwd=repo)
    out, err = capsys.readouterr()
    return rc, out, err


def test_merge_land_v5_prints_the_repository_check(env, monkeypatch, capsys):
    """A v5 front lands with its repository check, not ``[merge] check``."""
    repo = make_repo(env)
    write_merge_fallback(env, "false")
    brief_dir = env / "v5desk"
    brief_dir.mkdir()
    (brief_dir / "brief.toml").write_text(
        V5_BRIEF.format(name="v5desk", url=str(repo)).replace(
            "[[repository]]", 'merge = "desk"\n\n[[repository]]'),
        encoding="utf-8")
    assert fronts.front_add_main(str(brief_dir)) == 0
    capsys.readouterr()
    make_branch(repo, "feat", "feat.txt")
    seed_roster(session_record(SUP, "supervisor", "v5desk"),
                session_record(DESK, "merge-desk"))
    task = seed_built_task("v5desk")
    rc, out, err = request_take_land(monkeypatch, capsys, repo, "v5desk", task)
    assert rc == 0, err
    assert "check: true (repository foreman)" in out
    assert "[merge] fallback" not in out


def test_merge_land_old_front_prints_the_fallback(env, monkeypatch, capsys):
    """An old-shape front still lands on ``[merge] check`` and says so."""
    repo = make_repo(env)
    write_merge_fallback(env, "true")
    brief_dir = env / "olddesk"
    brief_dir.mkdir()
    (brief_dir / "brief.toml").write_text(
        OLD_BRIEF.format(name="olddesk").replace(
            'reviews   = "on request"',
            'reviews   = "on request"\nmerge     = "desk"'),
        encoding="utf-8")
    assert fronts.front_add_main(str(brief_dir)) == 0
    capsys.readouterr()
    make_branch(repo, "feat", "feat.txt")
    seed_roster(session_record(SUP, "supervisor", "olddesk"),
                session_record(DESK, "merge-desk"))
    task = seed_built_task("olddesk")
    rc, out, err = request_take_land(monkeypatch, capsys, repo, "olddesk", task)
    assert rc == 0, err
    assert "check: true ([merge] fallback)" in out
    assert "repository " not in out.split("check:", 1)[-1].split("\n", 1)[0]
