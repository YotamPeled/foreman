"""`foreman status`: the one screen as plain text.

The golden test drives the real CLI against the committed fixture with the
clock pinned and compares stdout to the committed expected output byte for
byte: any layout reorder, dropped block or recomputed number fails it. The
remaining tests name their own breaks: --fixture ignored, a crash on an
empty state directory, an empty heading, colour codes on the pipe.
"""

from __future__ import annotations

from pathlib import Path

from foreman import cli
from foreman.status import NOW_ENV

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = "tests/v0/fixture"
PINNED_NOW = "2026-09-08T12:00:00+00:00"

#: Section 13 order, and no other.
ORDER = ["Foreman status", "Needs you", "Problems", "Working",
         "Job queue", "Merge queue", "Capacity"]


def run_status(monkeypatch, capsys, argv):
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    assert cli.main(argv) == 0
    return capsys.readouterr().out


def test_golden_fixture_output(monkeypatch, capsys):
    """The full screen for the fixture, byte for byte. A reordered block,
    a reworded line or a recomputed age fails here; an intended layout
    change updates the golden file in the same commit."""
    out = run_status(monkeypatch, capsys, ["status", "--fixture", FIXTURE])
    expected = (ROOT / "tests" / "v0" / "status_expected.txt").read_text(
        encoding="utf-8")
    assert out == expected


def test_blocks_render_in_section_order(monkeypatch, capsys):
    """The owner's four questions top to bottom: needs-me above wrong,
    wrong above working, working above capacity, queues between."""
    out = run_status(monkeypatch, capsys, ["status", "--fixture", FIXTURE])
    positions = [out.index(heading) for heading in ORDER]
    assert positions == sorted(positions)


def test_fixture_flag_overrides_real_state(monkeypatch, capsys, tmp_path):
    """--fixture reads the given directory even when the real state
    directory exists and says something else. Pointing the environment at
    an empty state dir must not change one byte of fixture output."""
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    out = run_status(monkeypatch, capsys, ["status", "--fixture", FIXTURE])
    expected = (ROOT / "tests" / "v0" / "status_expected.txt").read_text(
        encoding="utf-8")
    assert out == expected


def test_empty_state_prints_nothing_lines(monkeypatch, capsys, tmp_path):
    """No state files at all: every block collapses to one line saying so,
    never an empty heading, and the v0-absent blocks stay out entirely."""
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "empty"))
    out = run_status(monkeypatch, capsys, ["status"])
    assert "collector never ticked" in out
    assert "Needs you: nothing needs you." in out
    assert "Problems: none." in out
    assert "Working: nothing running." in out
    assert "Job queue: empty." in out
    assert "Merge queue: empty." in out
    assert "Capacity: no collector data yet." in out
    assert "Component queue" not in out
    assert "Monitor" not in out


def test_status_carries_no_colour_codes(monkeypatch, capsys):
    """The panel renders this text later: no ANSI escapes on stdout, so a
    pipe gets exactly what a terminal gets."""
    out = run_status(monkeypatch, capsys, ["status", "--fixture", FIXTURE])
    assert "\x1b" not in out
    assert all(line == line.strip("\x00") for line in out.splitlines())
