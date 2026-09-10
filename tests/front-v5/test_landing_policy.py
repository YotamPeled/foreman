"""policy_for reads the repository policy; the global [merge] check is a fallback.

A v5 front's matching repository entry wins even when ``[merge] check``
says the opposite. An old-shape front, or a v5 front with no matching
entry, falls back. Neither having a check is a refusal that names the
front and the repository. ``foreman front policy`` prints the fields.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from foreman import cli, fronts, landing, paths, store
from foreman.caller import Refusal, SESSION_ENV

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
