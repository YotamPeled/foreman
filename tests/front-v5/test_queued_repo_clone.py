"""A queued job is cut from a clone Foreman made of the repository url,
never from the collector's working directory (which for the installed
unit is not a repository) and never from a checkout Foreman did not
create (decision 37)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from foreman import fronts, launch, paths
from foreman.caller import SESSION_ENV

V5_BRIEF = '''\
name = "clones"
goal = "A front whose repository is only a url."
finish-line = "Queued jobs are cut from Foreman's own clone."
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
work = "wclones"
target = "main"
check = "true"
land = "push"
'''

BUILT_BY_7_3G = pytest.mark.xfail(
    strict=True, reason="job-7.3g builds this; its worker removes this marker")


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=test@example.invalid",
         "-c", "user.name=foreman-test", *args],
        check=True, capture_output=True, text=True, cwd=cwd).stdout.strip()


@pytest.fixture()
def front(tmp_path, monkeypatch):
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
    git("-C", str(src), "branch", "wclones")
    bare = tmp_path / "remote.git"
    git("clone", "--bare", "-q", str(src), str(bare))
    url = bare.as_uri()  # a url, not a directory path
    brief = tmp_path / "brief"
    brief.mkdir()
    (brief / "brief.toml").write_text(V5_BRIEF.format(url=url),
                                      encoding="utf-8")
    monkeypatch.setattr(fronts, "_v5_remote_violations",
                        lambda *a, **k: [])  # the work branch exists already
    assert fronts.front_add_main(str(brief)) == 0
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(tmp_path / "unused")],
                   cwd=str(elsewhere), check=True)
    monkeypatch.chdir(elsewhere)
    return tmp_path


NODE = {"id": "job-1", "front": "clones", "kind": "job", "repo": "foreman",
        "role": "builder", "state": "queued"}


@BUILT_BY_7_3G
def test_a_url_repository_resolves_to_a_foreman_owned_clone(front):
    record = fronts.read_front_record("clones")
    path = Path(launch._queued_repo_path(record, NODE))
    assert path != Path.cwd()
    assert str(path).startswith(str(paths.state_dir()))
    assert git("-C", str(path), "rev-parse", "--verify", "refs/heads/wclones")


def test_a_directory_url_is_still_used_as_is(front, monkeypatch):
    record = dict(fronts.read_front_record("clones"))
    local = front / "remote.git"
    record["repositories"] = [dict(record["repositories"][0], url=str(local))]
    assert launch._queued_repo_path(record, NODE) == str(local)
