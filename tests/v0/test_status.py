"""`foreman status`: the one screen as plain text.

The golden test drives the real CLI against the committed fixture with the
clock pinned and compares stdout to the committed expected output byte for
byte: any layout reorder, dropped block or recomputed number fails it. The
remaining tests name their own breaks: --fixture ignored, a crash on an
empty state directory, an empty heading, colour codes on the pipe, a
missing estimate basis, a missing blocked line, a done front's ceiling in
Capacity, a missing Overall line.
"""

from __future__ import annotations

import json
from pathlib import Path

from foreman import cli
from foreman.status import NOW_ENV

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = "tests/v0/fixture"
PINNED_NOW = "2026-09-08T12:00:00+00:00"

#: Section 13 order, and no other.
ORDER = ["Foreman status", "Needs you", "Problems", "Working",
         "Job queue", "Merge queue", "Capacity"]


def run_status(monkeypatch, capsys, argv, tmp_path):
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    # The Capacity block reads the pool caps from the configuration at the
    # moment status runs, so the fixture pins an empty config directory and
    # the packaged defaults answer: without this the golden output would
    # follow whatever caps the machine running it happens to carry.
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    assert cli.main(argv) == 0
    return capsys.readouterr().out


def test_golden_fixture_output(monkeypatch, capsys, tmp_path):
    """The full screen for the fixture, byte for byte. A reordered block,
    a reworded line or a recomputed age fails here; an intended layout
    change updates the golden file in the same commit."""
    out = run_status(monkeypatch, capsys, ["status", "--fixture", FIXTURE],
                     tmp_path)
    expected = (ROOT / "tests" / "v0" / "status_expected.txt").read_text(
        encoding="utf-8")
    assert out == expected


def test_blocks_render_in_section_order(monkeypatch, capsys, tmp_path):
    """The owner's four questions top to bottom: needs-me above wrong,
    wrong above working, working above capacity, queues between."""
    out = run_status(monkeypatch, capsys, ["status", "--fixture", FIXTURE],
                     tmp_path)
    positions = [out.index(heading) for heading in ORDER]
    assert positions == sorted(positions)


def test_fixture_flag_overrides_real_state(monkeypatch, capsys, tmp_path):
    """--fixture reads the given directory even when the real state
    directory exists and says something else. Pointing the environment at
    an empty state dir must not change one byte of fixture output."""
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    out = run_status(monkeypatch, capsys, ["status", "--fixture", FIXTURE],
                     tmp_path)
    expected = (ROOT / "tests" / "v0" / "status_expected.txt").read_text(
        encoding="utf-8")
    assert out == expected


def test_empty_state_prints_nothing_lines(monkeypatch, capsys, tmp_path):
    """No state files at all: every block collapses to one line saying so,
    never an empty heading, and the v0-absent blocks stay out entirely."""
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "empty"))
    out = run_status(monkeypatch, capsys, ["status"], tmp_path)
    assert "collector never ticked" in out
    assert "Needs you: nothing needs you." in out
    assert "Problems: none." in out
    assert "Working: nothing running." in out
    assert "Job queue: empty." in out
    assert "Merge queue: empty." in out
    assert "Capacity: no collector data yet." in out
    assert "Front queue" not in out
    assert "Monitor" not in out


def test_status_carries_no_colour_codes(monkeypatch, capsys, tmp_path):
    """The panel renders this text later: no ANSI escapes on stdout, so a
    pipe gets exactly what a terminal gets."""
    out = run_status(monkeypatch, capsys, ["status", "--fixture", FIXTURE],
                     tmp_path)
    assert "\x1b" not in out
    assert all(line == line.strip("\x00") for line in out.splitlines())


def _jsonl(*records):
    return "".join(json.dumps(record) + "\n" for record in records)


def run_state(monkeypatch, capsys, tmp_path, files):
    """Status against a state directory built in tmp, clock pinned."""
    root = tmp_path / "state"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv(NOW_ENV, PINNED_NOW)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    monkeypatch.setenv("FOREMAN_STATE", str(root))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    assert cli.main(["status"]) == 0
    return capsys.readouterr().out


def test_front_with_rate_projects_with_basis(monkeypatch, capsys, tmp_path):
    """One job finished in the window: the ESTIMATE names the pace, and
    the want, remaining and unblocked lines all print."""
    out = run_state(monkeypatch, capsys, tmp_path, {
        "roster.json": json.dumps({"sessions": {}}),
        "fronts/pace/front.jsonl": _jsonl({
            "id": "front-pace", "name": "pace",
            "want": "The crew that paces the work.",
            "done_when": "Both jobs are done.", "allocation": {},
            "state": "active"}),
        "fronts/pace/tasks.jsonl": _jsonl(
            {"id": "t1", "front": "pace", "title": "Fast task",
             "state": "active", "units_done": 1, "units_total": 2},
            {"id": "t2", "front": "pace", "title": "Slow task",
             "state": "ready", "units_done": 0, "units_total": 2}),
        "fronts/pace/jobs.jsonl": _jsonl({
            "id": "j1", "task": "t1", "role": "muse", "state": "returned",
            "started_at": "2026-09-08T10:30:00+00:00",
            "returned_at": "2026-09-08T11:00:00+00:00"}),
    })
    assert "    want: The crew that paces the work." in out
    assert "    REMAINING (2): Fast task, Slow task" in out
    # Two tasks left at one job per 24h: a dated projection, not a guess.
    assert "    ESTIMATE: about 2d (1 job in last 24h)" in out
    assert "    Blocked on you: nothing blocked on you" in out


def test_front_without_rate_says_so(monkeypatch, capsys, tmp_path):
    """No job finished lately — a running job and a failed one carry no
    pace — so the ESTIMATE says "no rate yet" instead of guessing."""
    out = run_state(monkeypatch, capsys, tmp_path, {
        "roster.json": json.dumps({"sessions": {}}),
        "fronts/slow/front.jsonl": _jsonl({
            "id": "front-slow", "name": "slow",
            "want": "The crew still warming up.",
            "done_when": "One job is done.", "allocation": {},
            "state": "active"}),
        "fronts/slow/tasks.jsonl": _jsonl(
            {"id": "t1", "front": "slow", "title": "Only task",
             "state": "active", "units_done": 0, "units_total": 1}),
        "fronts/slow/jobs.jsonl": _jsonl(
            {"id": "j-run", "task": "t1", "role": "muse",
             "state": "running",
             "started_at": "2026-09-08T11:50:00+00:00"},
            {"id": "j-fail", "task": "t1", "role": "muse",
             "state": "failed",
             "started_at": "2026-09-08T09:00:00+00:00",
             "returned_at": "2026-09-08T11:00:00+00:00"}),
    })
    assert "    want: The crew still warming up." in out
    assert "    REMAINING (1): Only task" in out
    assert "    ESTIMATE: no rate yet \u2014 1 task remaining" in out


def test_blocked_lines_name_the_ask(monkeypatch, capsys, tmp_path):
    """An open inbox item from a front's supervisor prints under that
    front; the front nobody asked anything of says nothing is blocked."""
    roster = {"sessions": {
        "ses-sup-needy": {"id": "ses-sup-needy", "role": "supervisor",
                          "front": "needy", "state": "running"}}}
    front = _jsonl({
        "id": "front-needy", "name": "needy", "want": "The crew that asks.",
        "done_when": "One job is done.", "allocation": {},
        "state": "active"})
    tasks = _jsonl({"id": "t1", "front": "needy", "title": "Only task",
                    "state": "active", "units_done": 0, "units_total": 1})
    out = run_state(monkeypatch, capsys, tmp_path, {
        "roster.json": json.dumps(roster),
        "inbox.jsonl": _jsonl({
            "id": "inb-1", "from": "ses-sup-needy", "by": "ses-sup-needy",
            "kind": "money", "question": "Buy the map tiles?",
            "recommendation": "Yes.", "options": ["yes", "no"],
            "asked_at": "2026-09-08T11:46:00+00:00", "answered_at": None}),
        "fronts/needy/front.jsonl": front,
        "fronts/needy/tasks.jsonl": tasks,
        "fronts/needy/jobs.jsonl": "",
        "fronts/calm/front.jsonl": _jsonl({
            "id": "front-calm", "name": "calm",
            "want": "The crew that needs nothing.",
            "done_when": "One job is done.", "allocation": {},
            "state": "active"}),
        "fronts/calm/tasks.jsonl": _jsonl(
            {"id": "t9", "front": "calm", "title": "Quiet task",
             "state": "active", "units_done": 0, "units_total": 1}),
        "fronts/calm/jobs.jsonl": "",
    })
    assert ("    Blocked on you (1): Buy the map tiles? "
            "(money, 14m)") in out
    assert "    Blocked on you: nothing blocked on you" in out


def test_done_front_prints_no_allocation_lines(monkeypatch, capsys, tmp_path):
    """A front at state done keeps its Working block but lends no ceiling
    lines to Capacity; a live front's ceiling still prints."""
    files = {
        "roster.json": json.dumps({"sessions": {}}),
        "fronts/old/front.jsonl": _jsonl({
            "id": "front-old", "name": "old",
            "want": "Finished work.", "done_when": "It is done.",
            "allocation": {"muse": 2}, "state": "done"}),
        "fronts/old/tasks.jsonl": _jsonl(
            {"id": "t1", "front": "old", "title": "Past task",
             "state": "landed", "units_done": 1, "units_total": 1}),
        "fronts/old/jobs.jsonl": "",
        "fronts/live/front.jsonl": _jsonl({
            "id": "front-live", "name": "live",
            "want": "Current work.", "done_when": "It ships.",
            "allocation": {"muse": 2}, "state": "active"}),
        "fronts/live/tasks.jsonl": _jsonl(
            {"id": "t2", "front": "live", "title": "Next task",
             "state": "active", "units_done": 0, "units_total": 1}),
        "fronts/live/jobs.jsonl": "",
    }
    out = run_state(monkeypatch, capsys, tmp_path, files)
    assert "  live muse: 0/2 held" in out
    assert "old muse" not in out
    assert ("Overall: 1 of 2 fronts moving (1 done), "
            "no projected finish yet; needs you: nothing needs you.") in out


def test_overall_line_on_a_swarm_with_no_fronts(monkeypatch, capsys,
                                                tmp_path):
    """No fronts at all: the Overall line still closes the screen and
    says there is nothing, instead of going missing."""
    out = run_state(monkeypatch, capsys, tmp_path, {
        "roster.json": json.dumps({"sessions": {}}),
    })
    assert ("Overall: no fronts, no projected finish yet; "
            "needs you: nothing needs you.") in out


def test_new_lines_stay_within_the_readable_width(monkeypatch, capsys,
                                                  tmp_path):
    """want, REMAINING, ESTIMATE, Blocked and Overall clip at 100
    columns no matter how long the ledger's words run."""
    out = run_state(monkeypatch, capsys, tmp_path, {
        "roster.json": json.dumps({"sessions": {
            "ses-sup-long": {"id": "ses-sup-long", "role": "supervisor",
                             "front": "long", "state": "running"}}}),
        "inbox.jsonl": _jsonl({
            "id": "inb-long", "from": "ses-sup-long",
            "by": "ses-sup-long", "kind": "scope",
            "question": "Should we extend this very long question "
                        "with many more words than any terminal can "
                        "show on one line without clipping it short?",
            "recommendation": "No.", "options": [],
            "asked_at": "2026-09-08T11:00:00+00:00",
            "answered_at": None}),
        "fronts/long/front.jsonl": _jsonl({
            "id": "front-long", "name": "long",
            "want": "A want line that runs on and on with far more "
                    "words than fit beside its label on a terminal.",
            "done_when": "It ends.", "allocation": {},
            "state": "active"}),
        "fronts/long/tasks.jsonl": _jsonl(
            {"id": "t1", "front": "long",
             "title": "A task title that also runs far past any "
                      "reasonable width for one screen line",
             "state": "active", "units_done": 0, "units_total": 1}),
        "fronts/long/jobs.jsonl": "",
    })
    own = [line for line in out.splitlines()
           if line.startswith(("    want:", "    REMAINING",
                               "    ESTIMATE", "    Blocked", "Overall:"))]
    assert len(own) == 5
    assert all(len(line) <= 100 for line in own)


def test_the_overall_line_is_never_clipped(monkeypatch, capsys, tmp_path):
    """The line he reads first keeps its ending.

    Clipping took the tail, and the tail is what needs him: "needs you:
    nothing need..." says less than nothing. The detail is dropped before
    the sentence is.
    """
    from foreman import status as status_module

    rendered = run_status(monkeypatch, capsys,
                          ["status", "--fixture", FIXTURE], tmp_path)
    for line in rendered.splitlines():
        if line.startswith("Overall:"):
            assert len(line) <= status_module.LINE_WIDTH
            assert not line.endswith("...")
            assert line.endswith(".")
            return
    raise AssertionError("no Overall line on the screen")


def test_remaining_tasks_are_never_clipped():
    """A remaining task the owner cannot see is one he does not know is
    left, so a long list continues on an indented line rather than ending
    in an ellipsis. Seven real task titles overflow one terminal line.
    """
    from foreman import status as status_module

    titles = ["merge desk", "MCP server with role-scoped tools",
              "pools as plugin directories", "monitors",
              "doctor, hooks, migrations",
              "status: required fields and overall line", "the proof"]
    lines = status_module._wrapped(f"REMAINING ({len(titles)}):", titles)
    assert len(lines) > 1
    for line in lines:
        assert len(line) <= status_module.LINE_WIDTH
        assert not line.endswith("...")
    joined = " ".join(line.strip() for line in lines)
    for title in titles:
        assert title in joined
