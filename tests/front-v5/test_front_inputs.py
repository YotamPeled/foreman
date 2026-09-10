"""front add admits a v5-shape brief and leaves old briefs unchanged.

A brief is v5-shape when it carries ``goal``. These tests drive the real
``front add`` against a fresh FOREMAN_STATE, with a bare git repository
on tmp_path as the repository ``url`` (``main`` present, ``v5`` absent).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foreman import fronts, paths
from foreman.caller import SESSION_ENV


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
check = "python -m pytest tests -q"
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


def make_bare(root: Path) -> tuple[Path, str]:
    """A bare repo with ``main`` and no ``v5``. Returns (path, main sha)."""
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


def write_v5(root: Path, name: str, url: str, text: str | None = None) -> Path:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    body = V5_BRIEF.format(name=name, url=url) if text is None else text
    (brief_dir / "brief.toml").write_text(body, encoding="utf-8")
    return brief_dir


def add_refused(capsys, brief_dir: Path, *markers: str) -> str:
    rc = fronts.front_add_main(str(brief_dir))
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert err.count("refused:") == 1
    for marker in markers:
        assert marker in err, f"{marker!r} not named in: {err.strip()}"
    return err


def test_v5_shape_is_admitted_without_v1_keys(env, capsys):
    """A brief shaped like fronts/v5/FRONT.md is not refused for want."""
    bare, _sha = make_bare(env)
    brief = write_v5(env, "v5shape", str(bare))
    rc = fronts.front_add_main(str(brief), dry_run=True)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "want" not in err
    assert "done-when" not in err


def test_v5_missing_finish_line_is_refused(env, capsys):
    bare, _sha = make_bare(env)
    text = V5_BRIEF.format(name="nofinish", url=str(bare)).replace(
        'finish-line = "The front is admitted, recorded and old briefs still add."\n',
        "")
    add_refused(capsys, write_v5(env, "nofinish", str(bare), text),
                "finish-line")
    assert not paths.front_record_path("nofinish").exists()


def test_v5_two_sentence_finish_line_is_refused(env, capsys):
    bare, _sha = make_bare(env)
    text = V5_BRIEF.format(name="twosent", url=str(bare)).replace(
        'finish-line = "The front is admitted, recorded and old briefs still add."',
        'finish-line = "It works. Everyone cheers."')
    add_refused(capsys, write_v5(env, "twosent", str(bare), text),
                "finish-line")
    assert not paths.front_record_path("twosent").exists()


def test_v5_brief_with_task_is_refused(env, capsys):
    bare, _sha = make_bare(env)
    text = V5_BRIEF.format(name="withtask", url=str(bare)) + '''
[[task]]
title = "a leftover task"
scope = """
WHAT: leftover.
INPUTS: none.
OUTPUTS: none.
OUT OF SCOPE: everything else.
"""
verify = "true"
size = 1
'''
    add_refused(capsys, write_v5(env, "withtask", str(bare), text),
                "task")
    assert not paths.front_record_path("withtask").exists()


def test_v5_unknown_agent_is_refused_naming_every_pool(env, capsys):
    """An agent that is not a pool name, model or alias lists every pool."""
    bare, _sha = make_bare(env)
    text = V5_BRIEF.format(name="noagent", url=str(bare)).replace(
        '"opus-5:high:1:backup-builder"',
        '"no-such-agent:high:1:backup-builder"')
    err = add_refused(capsys, write_v5(env, "noagent", str(bare), text),
                      "team", "no-such-agent",
                      "grok-4.6", "opus-5", "astra-6", "muse-spark")
    assert "model grok-4.6" in err
    assert "model claude-opus-5" in err
    assert "aliases opus-5, opus" in err
    assert "aliases astra-6, astra" in err
    assert not paths.front_record_path("noagent").exists()


def test_v5_shape_defects_join_one_refusal(env, capsys):
    """Missing finish-line and a leftover [[task]] are named together."""
    bare, _sha = make_bare(env)
    text = V5_BRIEF.format(name="both", url=str(bare)).replace(
        'finish-line = "The front is admitted, recorded and old briefs still add."\n',
        "")
    text += '''
[[task]]
title = "leftover"
scope = "WHAT: x\\nINPUTS: y\\nOUTPUTS: z\\nOUT OF SCOPE: w"
verify = "true"
size = 1
'''
    add_refused(capsys, write_v5(env, "both", str(bare), text),
                "finish-line", "task")
    assert not paths.front_record_path("both").exists()
