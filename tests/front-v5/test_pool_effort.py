"""Pool effort_default: packaged values, pool list, launch default and floor.

The launcher's --effort absent uses the pool's default; Muse cannot run
below xhigh (ruling rul-frzifag). Tests that would be green without the
work are not in this file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foreman import cli
from foreman.caller import SESSION_ENV
from foreman.pool import pool_list_main
from foreman.pools import get as get_pool
from foreman.pools import plugins


SPEC_OK = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/front-v5 -q\n"
)

PACKAGED_EFFORT = {
    "muse": "xhigh",
    "grok": "high",
    "claude": "high",
    "codex": "low",
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    return tmp_path


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=path, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "test")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("add", "seed.txt")
    run("commit", "-qm", "seed")
    return path


def write_user_pool(name: str, body: str) -> Path:
    dest = Path(plugins.user_dir(name))
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.toml").write_text(body, encoding="utf-8")
    return dest


def test_pool_list_prints_each_packaged_pool_default_effort(env, capsys):
    """`pool list` prints effort after the timeout, one packaged value
    per pool. A list that still stopped at timeout would stay green
    without the key."""
    assert pool_list_main() == 0
    out = capsys.readouterr().out
    for name, effort in PACKAGED_EFFORT.items():
        line = next((row for row in out.splitlines()
                     if row.startswith(f"{name} ")), None)
        assert line is not None, f"no pool list line for {name}:\n{out}"
        assert f"timeout: {get_pool(name).timeout_default}" in line
        assert f"effort: {effort}" in line
        timeout_at = line.index("timeout:")
        effort_at = line.index("effort:")
        assert timeout_at < effort_at, line


def test_packaged_manifest_effort_matches_its_adapter(env):
    """The directory and the adapter must agree on the default, the way
    they already agree on the timeout. A key only on one side lists one
    pool and launches another."""
    for name, effort in PACKAGED_EFFORT.items():
        manifest = plugins.load_manifest(plugins.packaged_dir(name))
        assert manifest.effort_default == effort == get_pool(name).effort_default


def test_user_pool_may_omit_effort_default(env):
    """A user pool without the key still loads: it inherits nothing, and
    the launcher (not the loader) refuses a launch with no --effort."""
    write_user_pool(
        "barepool",
        'name = "barepool"\n'
        'model = "bare-model"\n'
        'timeout_default = "5m"\n'
        'interactive = false\n'
        'adapter = "muse"\n',
    )
    manifest = plugins.load_manifest(plugins.user_dir("barepool"))
    assert manifest.effort_default is None
    adapter = get_pool("barepool")
    assert adapter.effort_default is None


def test_invalid_effort_default_is_refused_like_timeout(env, tmp_path):
    """Present but not one of low/medium/high/xhigh is the same class of
    error as a timeout that does not parse: the directory does not load."""
    dest = tmp_path / "badpool"
    dest.mkdir()
    (dest / "manifest.toml").write_text(
        'name = "badpool"\n'
        'model = "x"\n'
        'timeout_default = "5m"\n'
        'effort_default = "soon"\n'
        'adapter = "muse"\n',
        encoding="utf-8",
    )
    with pytest.raises(plugins.InvalidManifest, match="effort_default") as raised:
        plugins.load_manifest(dest)
    assert "soon" in str(raised.value)
