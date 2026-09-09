"""The panel asks the runtime what it ships: the probe contract.

The panel's keys learn whether a verb exists by running
``foreman <verb> --help`` once and reading its exit status, never from a
list baked into the source. These tests pin both answers of that rule
against the checkout's own CLI, in an isolated world, and pin that no
hard-coded list of missing verbs remains in the plugin.

Each test names the break it catches: a probe that reads the wrong
answer for a shipped verb (the kill key goes quiet again), a probe that
reads the wrong answer for an unshipped verb (a key stops saying so),
and the stale list creeping back into the source (the next new verb
ships and the panel refuses it).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foreman import cli

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugin" / "foreman"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """The test names its own world and identity, inheriting no FOREMAN_*."""
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    return tmp_path


def test_probe_answer_for_a_verb_the_runtime_ships(world):
    """`foreman kill --help` exits 0: the probe reads shipped for kill,
    so the panel's kill key runs its verb instead of reporting it absent."""
    with pytest.raises(SystemExit) as exited:
        cli.main(["kill", "--help"])
    assert exited.value.code == 0


def test_probe_answer_for_a_verb_the_runtime_does_not_ship(world):
    """`foreman <bogus> --help` exits nonzero: the probe reads absent for
    a verb the runtime never registered, so a key for one still says so
    when pressed."""
    with pytest.raises(SystemExit) as exited:
        cli.main(["no-such-verb", "--help"])
    assert exited.value.code != 0


def test_no_hard_coded_missing_verb_list_in_the_plugin():
    """No QML file carries the stale missing-verbs list: the probe is the
    only thing that may say a verb is absent, so the next verb the
    runtime ships works the moment it lands."""
    stale = ("runsUnshipped",
             "Verbs this runtime does not ship",
             "was never added to the CLI")
    files = sorted(PLUGIN.rglob("*.qml"))
    assert files, "the panel plugin holds no QML files"
    for path in files:
        text = path.read_text(encoding="utf-8")
        for marker in stale:
            assert marker not in text, \
                "%s still hard-codes a missing verb: %r" % (path.name, marker)


def test_keys_asks_the_runtime_before_running():
    """Keys.qml probes the runtime (`--help`) instead of carrying the
    answer: deleting the probe while keeping the source green must fail
    aloud here, not silently in the panel."""
    text = (PLUGIN / "Keys.qml").read_text(encoding="utf-8")
    assert "--help" in text
