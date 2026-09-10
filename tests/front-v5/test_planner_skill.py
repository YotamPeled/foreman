"""The planner skill interviews for the v5 inputs and refuses to finish otherwise.

The break: a skill edit that drops a required field name, a refusal line,
the calibration ceiling, or an example that ``front add --dry-run`` no
longer accepts.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from foreman import fronts
from foreman.caller import SESSION_ENV
from foreman.fronts import _V5_REPO_REQUIRED

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "foreman-plan" / "SKILL.md"
EXAMPLES = ROOT / "skills" / "foreman-plan" / "examples"
CALIBRATION = ROOT / "docs" / "knowledge" / "team-calibration.md"

#: Top-level keys ``_validate`` / ``_validate_v5`` require on a v5 brief.
V5_TOP_REQUIRED = (
    "name", "goal", "finish-line", "decisions", "supervisor", "team",
    "repository",
)

REFUSALS = (
    "Refusing to finish: the goal is missing.",
    "Refusing to finish: the finish line is missing.",
    "Refusing to finish: the team is missing.",
    "Refusing to finish: the repository is missing.",
    "Refusing to finish: a finish-line clause names no command.",
)


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


def make_bare(root: Path, stem: str) -> Path:
    """A bare repo with ``main`` and no work branch."""
    src = root / f"{stem}-src"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    bare = root / f"{stem}.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src), str(bare)],
                   check=True)
    return bare


def bind_urls(text: str, remotes: list[str]) -> str:
    found = re.findall(r'^url\s*=\s*"[^"]+"$', text, re.M)
    assert len(found) == len(remotes), (
        f"brief has {len(found)} url lines, need {len(remotes)}")
    for old, remote in zip(found, remotes):
        text = text.replace(old, f'url = "{remote}"', 1)
    return text


def dry_run_example(env: Path, name: str, n_repos: int, capsys) -> None:
    src = EXAMPLES / name
    dest = env / name
    shutil.copytree(src, dest)
    remotes = [str(make_bare(env, f"{name}-{i}")) for i in range(n_repos)]
    brief = dest / "brief.toml"
    brief.write_text(
        bind_urls(brief.read_text(encoding="utf-8"), remotes),
        encoding="utf-8")
    assert fronts.front_add_main(str(dest), dry_run=True) == 0
    out, err = capsys.readouterr()
    assert "would write" in out
    assert err == ""


def test_single_repository_example_passes_dry_run(env, capsys):
    dry_run_example(env, "single-repository", 1, capsys)


def test_two_repository_example_passes_dry_run(env, capsys):
    dry_run_example(env, "two-repository", 2, capsys)


def test_two_repository_example_has_pr_policy():
    text = (EXAMPLES / "two-repository" / "brief.toml").read_text(
        encoding="utf-8")
    assert text.count("[[repository]]") == 2
    assert 'land = "pr"' in text
    assert "pr-body" in text


def test_old_examples_are_gone():
    assert not (EXAMPLES / "single-task").exists()
    assert not (EXAMPLES / "multi-task").exists()
    names = sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir())
    assert names == ["single-repository", "two-repository"]


def test_skill_names_every_repo_required_field():
    text = SKILL.read_text(encoding="utf-8")
    missing = [field for field in _V5_REPO_REQUIRED if field not in text]
    assert not missing, f"skill never names _V5_REPO_REQUIRED {missing}"


def test_skill_names_every_top_level_required_key():
    text = SKILL.read_text(encoding="utf-8")
    missing = [key for key in V5_TOP_REQUIRED if key not in text]
    assert not missing, f"skill never names top-level required {missing}"


def test_skill_contains_refusal_lines():
    text = SKILL.read_text(encoding="utf-8")
    missing = [line for line in REFUSALS if line not in text]
    assert not missing, f"skill lost refusal lines {missing}"


def test_skill_team_section_quotes_calibration_ceiling():
    knowledge = CALIBRATION.read_text(encoding="utf-8")
    paragraph = knowledge.split("\n\n")[1]
    assert paragraph.startswith("The owner's team line on a front is the ceiling.")
    skill = SKILL.read_text(encoding="utf-8")
    assert paragraph in skill
    assert skill.index("**Team**") < skill.index(paragraph)


def test_skill_is_one_file_under_300_lines():
    assert SKILL.is_file()
    lines = SKILL.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 300, f"skill is {len(lines)} lines"
