"""`foreman panel-feed`: panel.json holds what the panel used to fold itself.

The panel read the state directory and folded the eight blocks in QML. Now
the runtime folds them and the panel reads one file. That move is only
correct if the facts are the same facts, so the guard is a golden one: the
files under ``panel_before/`` are the whole model state the QML layer held
against each committed fixture *before* the change — every block, every row,
every field, plus every front — dumped off a run of the old
``plugin/foreman/Harness.qml`` and never derived from the code under test.

The break it catches: any fold that drifts. A count, an age, a rate, a
projected finish, how many tasks a front landed, which job hangs under which
task, which pool a role waits on, whether a supervisor reads as headless —
in either fixture, in either direction. A number that moves here is a
behaviour change, not a performance one.

One field is excluded and says why: the collector's own age is measured
against the wall clock rather than against the collector's tick, so it
cannot be a fixed expectation. panel.json carries the tick's timestamp
(``collectorAt``) and the panel ages it, which is what keeps a dead
collector from reading "alive" forever.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from foreman import panel_feed, paths

ROOT = Path(__file__).resolve().parents[2]
BEFORE = Path(__file__).resolve().parent / "panel_before"
FIXTURES = ROOT / "plugin" / "foreman" / "test"

#: What the old QML model exposed, and so what the golden holds.
RENDERED = ["header", "needsYou", "problems", "working", "jobQueue",
            "frontQueue", "mergeQueue", "capacity", "fronts", "now", "frozen"]

#: Measured against the wall clock, not the collector's tick: the panel
#: derives these itself from `collectorAt`, so they carry no expectation.
WALL_CLOCK = ("collectorAt", "collectorAgeS", "collectorAlive")


def rendered(feed: dict) -> dict:
    """The summary reduced to what the old model held, so the two compare."""
    header = {key: value for key, value in feed["header"].items()
              if key not in WALL_CLOCK}
    header["rows"] = [row for row in header["rows"]
                      if row["label"] != "collector"]
    out = {"header": header}
    for name in RENDERED[1:]:
        out[name] = feed[name]
    return out


@pytest.mark.parametrize("fixture", ["fixture", "fixture-live"])
def test_panel_json_holds_what_the_blocks_rendered(fixture, monkeypatch):
    """Every fact the QML model folded, folded now by the runtime instead."""
    monkeypatch.setenv("FOREMAN_STATE", str(FIXTURES / fixture))
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    want = json.loads((BEFORE / f"{fixture}.json").read_text())
    assert rendered(panel_feed.gather()) == want


@pytest.mark.parametrize("fixture", ["fixture", "fixture-live"])
def test_the_collector_age_is_left_for_the_panel(fixture, monkeypatch):
    """The one wall-clock fact travels as a timestamp, not as an age: a
    summary that froze the age would leave a dead collector reading alive."""
    monkeypatch.setenv("FOREMAN_STATE", str(FIXTURES / fixture))
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    header = panel_feed.gather()["header"]
    observed = json.loads((FIXTURES / fixture / "observed.json").read_text())
    assert header["collectorAt"] == observed["at"]


def test_the_summary_is_written_where_the_panel_reads_it(tmp_path,
                                                         monkeypatch):
    """`foreman panel-feed --fixture` writes panel.json into that directory
    and nowhere else, so a run against a fixture never reaches the machine's
    own state directory."""
    state = tmp_path / "state"
    shutil.copytree(FIXTURES / "fixture-live", state)
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "elsewhere"))
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)

    assert panel_feed.panel_feed_main(str(state)) == 0
    written = json.loads((state / "panel.json").read_text())
    assert written["header"]["registered"] == 3
    assert [row["name"] for row in written["fronts"]] == ["harbor", "quay"]
    assert not (tmp_path / "elsewhere").exists()
    # The world it was handed back is the world it was called with.
    assert paths.state_dir() == tmp_path / "elsewhere"


def test_a_missing_fixture_is_refused(tmp_path, capsys):
    """A path that is not a directory is named and refused, never folded
    against whatever the ambient state happens to be."""
    assert panel_feed.panel_feed_main(str(tmp_path / "nowhere")) == 1
    assert "no such fixture directory" in capsys.readouterr().out


def test_an_empty_state_directory_folds_to_empty_blocks(tmp_path,
                                                        monkeypatch):
    """No roster, no ledgers, no fronts: eight empty blocks and no error.
    The panel opens on a machine where nothing has run yet."""
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    feed = panel_feed.gather()
    for name in ("needsYou", "problems", "working", "jobQueue",
                 "frontQueue", "mergeQueue", "capacity"):
        assert feed[name]["count"] == 0, name
    assert feed["header"]["registered"] == 0
    assert feed["header"]["collectorAt"] == ""
    assert feed["fronts"] == []


def test_a_verb_that_writes_leaves_the_summary_written(tmp_path,
                                                       monkeypatch):
    """The panel reads one file, so a verb that moved a ledger has to move
    that file too — and a verb that wrote nothing must not."""
    from foreman import cli

    state = tmp_path / "state"
    shutil.copytree(FIXTURES / "fixture-live", state)
    monkeypatch.setenv("FOREMAN_STATE", str(state))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)

    summary = state / "panel.json"
    assert not summary.exists()
    assert cli.main(["status"]) == 0
    assert not summary.exists(), "a read-only verb wrote the summary"

    assert cli.main(["answer", "inb-hb001", "north"]) == 0
    assert json.loads(summary.read_text())["needsYou"]["count"] == 1
