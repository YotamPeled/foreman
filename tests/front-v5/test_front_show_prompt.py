"""The supervisor prompt carries v5 inputs; both templates render closed.

A summon of a v5 front (the dry-run path ``tests/v1/test_summon.py`` uses)
must write a role prompt that names the goal, the finish line, decision 1
verbatim, every team line and every repository's check. An old-shape
front still points at its tasks. No ``{{`` survives in either supervisor
template.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foreman import cli, fronts, launch, paths, store
from foreman.caller import SESSION_ENV

ROOT = Path(__file__).resolve().parents[2]
PANEL_BRIEF = ROOT / "briefs" / "panel"

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
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


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


def add_v5(env: Path, name: str = "v5shape") -> dict:
    bare, _sha = make_bare(env)
    assert fronts.front_add_main(str(write_v5(env, name, str(bare)))) == 0
    return store.fold_by_id(
        store.read_ledger(paths.front_record_path(name)))[0]


def line(out: str, prefix: str) -> str:
    for row in out.splitlines():
        if row.startswith(prefix):
            return row.split(prefix, 1)[1].strip()
    raise AssertionError(f"no {prefix!r} line in out:\n{out}")


def team_line(entry: dict) -> str:
    return (f"- role {entry['role']}, agent {entry['agent']}, "
            f"pool {entry['pool']}, model {entry['model']}, "
            f"effort {entry['effort']}, count {entry['count']}")


def assert_v5_inputs(text: str, record: dict) -> None:
    """Goal, finish line, decision 1, every team line, every repo check."""
    assert record["goal"] in text
    assert record["finish_line"] in text
    assert f"1. {record['decisions'][0]}" in text
    for entry in record["team"]:
        assert team_line(entry) in text, entry
    for repo in record["repositories"]:
        assert repo["check"] in text, repo
    assert "## Goal" in text
    assert "## Finish line" in text
    assert "## Decisions" in text
    assert "## Team" in text
    assert "## Repositories" in text


def test_v5_summon_dry_run_prompt_carries_the_inputs(env, capsys):
    """A dry-run summon of a v5 front writes the inputs into the prompt."""
    record = add_v5(env)
    capsys.readouterr()
    rc = cli.main(["launch", "supervisor", "v5shape", "--workspace", "6",
                   "--repo", str(env), "--dry-run"])
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    prompt = Path(line(out, "role prompt: ")).read_text(encoding="utf-8")
    assert "{{" not in prompt
    assert_v5_inputs(prompt, record)
    assert fronts.front_inputs_block(record) in prompt
    assert "(a v5 front has no brief tasks; its work is the tree)" in prompt
    assert "(this front has no tasks on the ledger)" not in prompt
    assert "(this front was started from an old-shape brief" not in prompt


def test_both_supervisor_templates_render_without_a_hole(env):
    """Windowed and headless both close every ``{{field}}``."""
    record = add_v5(env)
    role_prompt = env / "role-prompt.md"
    for headless in (False, True):
        text = launch.render_supervisor_prompt(
            front="v5shape", record=record, session_id="ses-tmpl-v5",
            repo=str(env), branch="main", role_prompt=role_prompt,
            predecessor=None, headless=headless)
        assert "{{" not in text, f"headless={headless}"
        assert "}}" not in text, f"headless={headless}"
        assert_v5_inputs(text, record)
        assert "(a v5 front has no brief tasks; its work is the tree)" in text


def test_old_shape_prompt_points_at_its_tasks(env, capsys):
    """An old-shape front keeps the parenthetical and its task lines."""
    assert cli.main(["front", "add", str(PANEL_BRIEF)]) == 0
    capsys.readouterr()
    rc = cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                   "--repo", str(env), "--dry-run"])
    assert rc == 0, capsys.readouterr().err
    prompt = Path(line(capsys.readouterr().out, "role prompt: ")).read_text(
        encoding="utf-8")
    assert "{{" not in prompt
    assert fronts.OLD_SHAPE_INPUTS in prompt
    assert '(a v5 front has no brief tasks; its work is the tree)' not in prompt
    assert '- "plugin skeleton and data feed" — ready' in prompt
