"""Paths come from the environment; the CLI prints the version."""

import os
from pathlib import Path

from foreman import __version__, cli, paths


def test_state_and_config_follow_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "s"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "c"))
    assert paths.state_dir() == tmp_path / "s"
    assert paths.roster_path() == tmp_path / "s" / "roster.json"
    assert paths.observed_path().parent == tmp_path / "s"
    assert paths.front_tasks_path("corpus") == (
        tmp_path / "s" / "fronts" / "corpus" / "tasks.jsonl"
    )
    assert paths.front_map_path("corpus") == (
        tmp_path / "s" / "fronts" / "corpus" / "map.jsonl"
    )
    assert paths.front_milestones_path("corpus") == (
        tmp_path / "s" / "fronts" / "corpus" / "milestones.jsonl"
    )
    assert paths.checkpoint_path("ses-1") == (
        tmp_path / "s" / "sessions" / "ses-1" / "checkpoint.json"
    )
    assert paths.config_file() == tmp_path / "c" / "foreman.toml"
    assert paths.brief_path("corpus").parent == tmp_path / "c" / "fronts" / "corpus"
    root = paths.ensure_state_tree()
    assert root.is_dir()
    assert (root / "fronts").is_dir()
    assert (root / "sessions").is_dir()
    assert (root / "events").is_dir()


def test_defaults_without_environment(monkeypatch):
    monkeypatch.delenv("FOREMAN_STATE", raising=False)
    monkeypatch.delenv("FOREMAN_CONFIG", raising=False)
    assert paths.state_dir() == Path(os.path.expanduser("~/.local/state/foreman"))
    assert paths.config_dir() == Path(os.path.expanduser("~/.config/foreman"))


def test_version_subcommand(capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__ == "0.0.1"
