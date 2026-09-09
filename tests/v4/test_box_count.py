"""Capacity counts each pool's vendor processes on the whole machine.

Every test drives the count with a fake process table. None of them read
this machine. The break each test catches is in its docstring.
"""

from __future__ import annotations

import pytest

from foreman import capacity, cli, pools
from foreman.caller import SESSION_ENV
from foreman.pools import PoolAdapter


#: A real grok run plus the shapes that fooled ``pgrep -f``: tee, a bash
#: wrapper whose command text contains the word, and another vendor
#: session whose prompt mentions it.
PGREP_FOOLS = {
    11: {"cmdline": "grok --prompt-file FOREMAN-JOB.md -m grok-4.6"},
    12: {"cmdline": "tee session.log"},
    13: {"cmdline": "bash -c echo $$ > pid; grok --prompt-file job.md"},
    14: {"cmdline": "claude -p --model opus the prompt mentions grok workers"},
}

THREE_GROK = {
    21: {"cmdline": "grok --prompt-file a.md"},
    22: {"cmdline": "/usr/bin/grok --prompt-file b.md"},
    23: {"cmdline": "grok"},
}

ONE_GROK = {
    31: {"cmdline": "grok --prompt-file a.md"},
}

TWO_GROK = {
    41: {"cmdline": "grok --prompt-file a.md"},
    42: {"cmdline": "grok --prompt-file b.md"},
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    return tmp_path


def test_the_count_matches_argv0_not_a_substring(env):
    """``pgrep -f grok`` counted tee, a bash wrapper, a prompt that
    mentioned the word, and the grep itself. The box count walks argv[0]'s
    basename and must return one for that table, not four."""
    counts = capacity.running_by_pool(PGREP_FOOLS)

    assert counts["grok"] == 1
    assert counts["claude"] == 1
    assert counts["muse"] == 0
    assert counts["codex"] == 0


def test_a_pool_with_no_binary_counts_nothing(env):
    """A pool that names no vendor binary must count zero even when the
    table is full, never every process. Matching the empty string against
    argv[0] would count the world."""

    class Nameless(PoolAdapter):
        name = "nameless"

    pools.register("nameless", Nameless())
    try:
        counts = capacity.running_by_pool(PGREP_FOOLS)
        assert counts["nameless"] == 0
        assert counts["grok"] == 1
    finally:
        pools.unregister("nameless")


def test_a_launch_is_refused_when_the_box_is_at_the_cap(env):
    """Two of three slots held is room on the ledger; three grok
    processes on the machine is not. The refusal names the box, not
    the slot count. The same launch against an empty table is admitted."""
    assert cli.main(["cap", "grok", "3"]) == 0
    capacity.grant(pool="grok", front="runtime-v4", role="grok",
                   job="job-held1", session="ses-held0001")
    capacity.grant(pool="grok", front="runtime-v4", role="grok",
                   job="job-held2", session="ses-held0002")

    refused = capacity.launch_problems(
        "grok", "grok", "runtime-v4", table=THREE_GROK)
    assert refused == [
        "role 'grok' on front 'runtime-v4': 3 grok processes on the box, "
        "cap 3",
    ]
    assert "held" not in refused[0]

    admitted = capacity.launch_problems(
        "grok", "grok", "runtime-v4", table={})
    assert admitted == []


def test_capacity_lines_print_the_box_count_when_it_differs(env):
    """The row stays ``n/m held`` when the two numbers agree, and gains
    ``· k running on the box`` when they do not. A suffix on the agree
    case is a number the owner already has; hiding the disagree case is
    the lie this count exists to stop."""
    assert cli.main(["cap", "grok", "3"]) == 0
    capacity.grant(pool="grok", front="runtime-v4", role="grok",
                   job="job-held1", session="ses-held0001")

    differ = capacity.capacity_lines({}, table=TWO_GROK)
    assert "  grok: 1/3 held · 2 running on the box" in differ

    agree = capacity.capacity_lines({}, table=ONE_GROK)
    assert "  grok: 1/3 held" in agree
    assert "running on the box" not in "\n".join(agree)
