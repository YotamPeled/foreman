"""`foreman status` in the v5 shape: queue, capacity, milestones, tree, landings.

Every test drives the real CLI against a fresh FOREMAN_STATE. Front
records are written directly so the tests name the team they need;
``front add`` is not the seam. Capacity's process table is substituted
so the box count does not read this machine.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman import capacity, cli, entities, ids, paths, store
from foreman.caller import SESSION_ENV
from foreman.status import NOW_ENV

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
PINNED_NOW = "2026-09-10T12:00:00+00:00"
OLD_PINNED_NOW = "2026-09-08T12:00:00+00:00"
ROOT = Path(__file__).resolve().parents[2]
V5_FIXTURE = "tests/v0/fixture-v5"
V5_EXPECTED = ROOT / "tests" / "v0" / "status_expected_v5.txt"
OLD_FIXTURE = "tests/v0/fixture"
OLD_EXPECTED = ROOT / "tests" / "v0" / "status_expected.txt"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.setattr(capacity, "_snapshot", lambda: {})
    return tmp_path


def write_v5(name: str, builders: int = 1, pool: str = "grok",
             prefer: int = 0, state: str = "queued",
             behind: str = "") -> dict:
    paths.front_dir(name).mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "grok-4.6", "pool": pool, "model": "grok-4.6",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "grok-4.6", "pool": pool, "model": "grok-4.6",
         "effort": "high", "count": builders, "role": "builder"},
    ]
    line = entities.Front(
        id=ids.mint("front"), name=name, state=state, shape="v5",
        prefer=prefer, behind=behind,
        goal="Show the v5 screen.", finish_line="The queue names both.",
        allocation={"grok": builders}, team=team,
        repositories=[{"name": "foreman", "target": "main",
                       "work": "v5", "base": "main",
                       "url": "https://example.invalid/foreman.git"}],
    ).to_dict()
    store.append_ledger(paths.front_record_path(name), line)
    return line


def append_tree(front: str, *records: dict) -> None:
    for record in records:
        store.append_ledger(paths.front_tree_path(front), record)


def status_out(monkeypatch, capsys) -> str:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert cli.main(["status"]) == 0
    return capsys.readouterr().out


def queue_lines(out: str) -> list[str]:
    """The swarm ``queue:`` block, heading included, up to Needs you."""
    lines = out.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == "queue:" or line.startswith("queue: "):
            start = index
            break
    assert start is not None, f"no queue block in:\n{out}"
    end = len(lines)
    for index, line in enumerate(lines[start + 1:], start + 1):
        if line.startswith("Needs you"):
            end = index
            break
    return lines[start:end]


def test_queue_block_names_both_fronts_with_their_reasons(env, monkeypatch,
                                                          capsys):
    """Two queued v5 fronts: the top is team fits, the second is behind it."""
    write_v5("alpha", builders=1)
    write_v5("beta", builders=1)
    out = status_out(monkeypatch, capsys)
    assert queue_lines(out) == [
        "queue:",
        "  alpha  team fits",
        "  beta  behind alpha",
    ]


def test_queue_block_names_a_running_front_after_the_queued_ones(
        env, monkeypatch, capsys):
    """A queued front keeps its wait reason; an active front is running."""
    write_v5("harbor", builders=1, state="queued")
    write_v5("orbit", builders=1, state="active")
    out = status_out(monkeypatch, capsys)
    assert queue_lines(out) == [
        "queue:",
        "  harbor  team fits",
        "  orbit  running",
    ]


def test_capacity_line_prints_held_reserved_and_cap(env, monkeypatch, capsys):
    """After a reserve, Capacity spells held / reserved / cap per pool."""
    write_v5("orbit", builders=1, state="active")
    assert cli.main(["front", "reserve", "orbit"]) == 0
    capsys.readouterr()
    out = status_out(monkeypatch, capsys)
    assert "held 0 / reserved 1 / cap 1" in out


def tree_body(out: str) -> list[str]:
    """Status tree lines with the front-block indent stripped."""
    lines = out.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == "    tree:":
            start = index + 1
            break
    assert start is not None, f"no tree block in:\n{out}"
    stop_prefixes = (
        "landings:", "want:", "REMAINING", "ESTIMATE", "Blocked",
        "doing now", "progress", "allocation:", "evidence:", "queue:",
    )
    body = []
    for line in lines[start:]:
        if not line.startswith("    "):
            break
        rest = line[4:]
        if rest.startswith("M") and "pieces (split from" in rest:
            break
        if any(rest == prefix or rest.startswith(prefix)
               for prefix in stop_prefixes):
            break
        if rest.startswith("  "):
            rest = rest[2:]
        body.append(rest)
    return body


def test_milestone_line_prints_pieces_and_split_count_after_split(
        env, monkeypatch, capsys):
    """After ``milestone split``, each live part names pieces and split from."""
    write_v5("orbit", state="active")
    assert cli.main([
        "milestone", "add", "orbit",
        "--title", "The screen",
        "--done-when", "status shows the v5 shape",
        "--verify", "python -m pytest tests -q",
        "--reason", "decision 15",
        "--break", "drop the split count",
    ]) == 0
    source = capsys.readouterr().out.strip()
    assert cli.main([
        "milestone", "split", "orbit", source,
        "--into", "Part A", "--part",
        "Part A|done when A|python -m pytest tests -q|break A",
        "--into", "Part B", "--part",
        "Part B|done when B|python -m pytest tests -q|break B",
        "--reason", "split for size",
    ]) == 0
    part_a, part_b = capsys.readouterr().out.strip().split()
    append_tree("orbit",
                {"id": "tsk-a", "parent": part_a, "kind": "task",
                 "title": "first piece", "state": "landed"},
                {"id": "tsk-b", "parent": part_a, "kind": "task",
                 "title": "second piece", "state": "ready"})
    out = status_out(monkeypatch, capsys)
    assert "M1 Part A: 1/2 pieces (split from 2)" in out
    assert "M2 Part B: 0/0 pieces (split from 2)" in out


def test_tree_block_matches_node_list_line_for_line(env, monkeypatch, capsys):
    """The tree block is the checker fold, one status line per node-list line."""
    write_v5("orbit", state="active")
    append_tree(
        "orbit",
        {"id": "mil-1", "front": "orbit", "parent": "orbit",
         "kind": "milestone", "title": "The screen"},
        {"id": "tsk-1", "parent": "mil-1", "kind": "task",
         "title": "status shape", "state": "queued"},
        {"id": "job-1", "parent": "tsk-1", "kind": "job",
         "title": "implement it", "state": "queued", "waits": "ready"},
    )
    assert cli.main(["node", "list", "orbit"]) == 0
    node_lines = [line for line in capsys.readouterr().out.splitlines()
                  if line.strip()]
    out = status_out(monkeypatch, capsys)
    got = tree_body(out)
    assert len(got) == len(node_lines), f"{got!r} vs {node_lines!r}\n{out}"
    for status_line, node_line in zip(got, node_lines):
        assert status_line.startswith(node_line), (
            f"{status_line!r} does not start with {node_line!r}")
    queued = [line for line in got if "job-1" in line]
    assert queued and "waits: ready" in queued[0]


def test_landing_item_and_behind_warning_appear(env, monkeypatch, capsys):
    """A script landing item prints its state; a behind sha warns with target."""
    write_v5("orbit", state="active",
             behind="abcdef1234567890abcdef1234567890")
    append_tree(
        "orbit",
        {"id": "mil-1", "front": "orbit", "parent": "orbit",
         "kind": "milestone", "title": "The screen"},
        {"id": "job-land1", "parent": "mil-1", "kind": "job",
         "title": "land implement it", "role": "script",
         "lands": "job-1", "state": "queued", "waits": "ready"},
    )
    out = status_out(monkeypatch, capsys)
    assert "      job-land1  queued" in out
    assert "      behind main (abcdef1)" in out


def test_golden_v5_fixture_output(monkeypatch, capsys, tmp_path):
    """The committed v5 fixture, byte for byte."""
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setattr(capacity, "_snapshot", lambda: {})
    assert cli.main(["status", "--fixture", V5_FIXTURE]) == 0
    out = capsys.readouterr().out
    expected = V5_EXPECTED.read_text(encoding="utf-8")
    assert out == expected


def test_old_fixture_text_is_unchanged(monkeypatch, capsys, tmp_path):
    """The v0 golden file still matches the old fixture byte for byte."""
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv(NOW_ENV, OLD_PINNED_NOW)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setattr(capacity, "_snapshot", lambda: {
        101: {"cmdline": "grok --prompt-file job.md"},
        102: {"cmdline": "muse exec --prompt-file job.md"},
    })
    assert cli.main(["status", "--fixture", OLD_FIXTURE]) == 0
    out = capsys.readouterr().out
    expected = OLD_EXPECTED.read_text(encoding="utf-8")
    assert out == expected
