"""Decision 36: every verb the decisions give the foreman, the foreman may
call. Named in the decision as refused wrongly: front prefer, front
import, front reserve, ask, finding, cap. A verb may still refuse for
another reason (a missing directory); only the role refusal is judged."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from foreman import cli, fronts, paths, store
from foreman.caller import SESSION_ENV
from foreman.entities import Session

FOREMAN_SES = "ses-for0001"
WORKER = "ses-wrk0001"

V5_BRIEF = '''\
name = "verbs"
goal = "A front the foreman orders and reserves."
finish-line = "The foreman calls the verbs its decisions give it."
decisions = ["Everything is a CLI verb."]
supervisor = "opus-5:high"
team = [
  "opus-5:high:1:supervisor",
  "opus-5:high:1:builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "wverbs"
target = "main"
check = "true"
land = "push"
'''


def git(*args: str) -> None:
    subprocess.run(["git", "-c", "user.email=test@example.invalid",
                    "-c", "user.name=foreman-test", *args],
                   check=True, capture_output=True)


def session(sid: str, role: str, **fields) -> dict:
    base = {
        "id": sid, "role": role, "pool": "claude", "model": "m",
        "front": None, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "", "launched_by": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_declared_at": None, "last_observed_at": None,
        "cpu_s": 0.0, "state": "running",
    }
    base.update(fields)
    return Session.from_dict(base).to_dict()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    git("init", "-b", "main", "-q", str(tmp_path))
    git("-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init")
    src = tmp_path / "src-repo"
    src.mkdir()
    git("init", "-b", "main", "-q", str(src))
    git("-C", str(src), "commit", "-q", "--allow-empty", "-m", "init")
    bare = tmp_path / "remote.git"
    git("clone", "--bare", "-q", str(src), str(bare))
    brief = tmp_path / "brief"
    brief.mkdir()
    (brief / "brief.toml").write_text(V5_BRIEF.format(url=bare),
                                      encoding="utf-8")
    assert fronts.front_add_main(str(brief)) == 0
    store.write_snapshot(paths.roster_path(), {"sessions": {
        FOREMAN_SES: session(FOREMAN_SES, "foreman"),
        WORKER: session(WORKER, "muse", front="verbs"),
    }})
    return tmp_path


def call_as(monkeypatch, capsys, sid: str, argv: list[str]) -> str:
    monkeypatch.setenv(SESSION_ENV, sid)
    try:
        cli.main(argv)
    except SystemExit:
        pass
    captured = capsys.readouterr()
    return captured.out + captured.err


FOREMAN_VERBS = {
    "front prefer": ["front", "prefer", "verbs", "3"],
    "front import": ["front", "import", "verbs", "no-such-directory",
                     "--dry-run"],
    "front reserve": ["front", "reserve", "verbs"],
    "ask": ["ask", "--kind", "money", "--recommend", "yes",
            "May the verbs front spend five dollars on meters?"],
    "finding": ["finding", "--on", "verbs", "--class", "probe",
                "--title", "a probe finding on front verbs",
                "--detail", "written by the role test"],
    "cap": ["cap", "claude", "2"],
}


BUILT_BY_8_3A = pytest.mark.xfail(
    strict=True, reason="job-8.3a builds this; its worker removes this marker")
PERMITTED_TODAY = {"front reserve"}


@pytest.mark.parametrize("verb", [
    verb if verb in PERMITTED_TODAY else pytest.param(verb, marks=BUILT_BY_8_3A)
    for verb in sorted(FOREMAN_VERBS)])
def test_the_foreman_may_call_the_verbs_its_decisions_give_it(
        world, monkeypatch, capsys, verb):
    shown = call_as(monkeypatch, capsys, FOREMAN_SES, FOREMAN_VERBS[verb])
    assert "role 'foreman' may not call" not in shown, shown


def test_a_worker_is_still_refused_front_prefer(world, monkeypatch, capsys):
    shown = call_as(monkeypatch, capsys, WORKER, FOREMAN_VERBS["front prefer"])
    assert "may not call 'front prefer'" in shown, shown
