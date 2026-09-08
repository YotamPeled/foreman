"""`foreman front allocate` and a cap that takes effect on the screen at once.

Every test drives the real entry points against a fresh FOREMAN_STATE and
FOREMAN_CONFIG directory, then reads the ledgers back. The breaks worth
naming up front: a ceiling changed by a hand-written front line that drops
`state` and `prefer` (folding is last-wins over whole records, so the front
loses both); a ceiling lowered below what the front holds (a silent
over-subscription); and a Capacity block read from the collector's cached
copy, which keeps showing the old ceiling after `foreman cap` until the
collector is restarted.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from foreman import capacity, cli, entities, paths, store
from foreman.caller import SESSION_ENV
from foreman.collector import load_config, tick

#: A minimal brief that validates: two allocated roles and one monitor, so
#: the whole-record test has fields beyond the allocation to lose.
BRIEF = '''name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 2
opus = 1

[[task]]
title = "first work"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "make check first"
size = 3
after = []

[[monitor]]
question = "how much is done?"
measure = "count-things"
unit = "things"
every = "landing"
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    # `front add` refuses a `land-on` branch it cannot find, so every brief
    # written under tmp_path lives in a throwaway repository with a `main`
    # branch.
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


def add_front(root: Path, name: str) -> None:
    brief_dir = root / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "brief.toml").write_text(BRIEF.format(name=name),
                                          encoding="utf-8")
    assert cli.main(["front", "add", str(brief_dir)]) == 0


def safe_tick() -> None:
    """One collector tick with the file's caps and no vendor markers.

    The file's `[pool.*]` caps reach the tick (that is what is under test)
    while the default vendor binary names stay out of the intruder scan, so
    a developer machine running a matching binary cannot flake the suite.
    """
    tick(config=dataclasses.replace(
        load_config(), vendor_markers=("no-such-vendor",)))


def test_allocate_appends_a_revised_copy_of_the_whole_record(env, capsys):
    """The appended line carries every field the previous one had.

    The ceiling used to change by a hand-written front line, and the
    hand-written line carried the allocation and not `state` or `prefer` —
    folding is last-wins over whole records, so `front list` went from
    "queued · prefer 5" to "None · prefer 0". This test compares the new
    line field by field against the old one, so dropping any single field
    fails it.
    """
    add_front(env, "alpha")
    capsys.readouterr()
    before_bytes = paths.front_record_path("alpha").read_bytes()
    before = store.read_ledger(paths.front_record_path("alpha"))
    assert len(before) == 1

    assert cli.main(["front", "allocate", "alpha", "muse", "5"]) == 0
    assert capsys.readouterr().out.strip() == "alpha: muse ceiling 5"

    after_bytes = paths.front_record_path("alpha").read_bytes()
    assert after_bytes.startswith(before_bytes)
    assert len(after_bytes) > len(before_bytes)
    after = store.read_ledger(paths.front_record_path("alpha"))
    assert len(after) == len(before) + 1

    latest = store.fold_by_id(after)[0]
    assert set(latest) == set(before[0]), \
        "the revised copy must carry exactly the old fields"
    # `at`/`by` are the store's per-append envelope, not record content: the
    # new line carries its own stamp. Everything else must be identical.
    assert (before[0]["by"], latest["by"]) == ("owner", "owner")
    for key, value in before[0].items():
        if key in ("allocation", "at", "by"):
            continue
        assert latest[key] == value, f"field {key!r} was not carried over"
    assert latest["allocation"] == {"muse": 5, "opus": 1}
    assert latest["id"] == before[0]["id"]
    assert latest["state"] == "queued"
    assert latest["prefer"] == 0
    assert latest["monitors"][0]["measure"] == "count-things"
    assert capacity.ceiling("alpha", "muse") == 5


def test_allocate_can_add_a_role_the_brief_never_named(env, capsys):
    """A served role missing from the brief's allocation gains a ceiling.

    Replacing the allocation table instead of copying it would drop the two
    roles the brief did name; the literals below catch either direction.
    """
    add_front(env, "alpha")
    capsys.readouterr()

    assert cli.main(["front", "allocate", "alpha", "grok", "3"]) == 0
    capsys.readouterr()

    record = store.fold_by_id(
        store.read_ledger(paths.front_record_path("alpha")))[0]
    assert record["allocation"] == {"muse": 2, "opus": 1, "grok": 3}
    assert capacity.ceiling("alpha", "grok") == 3


def test_allocate_refuses_a_ceiling_below_what_is_held(env, capsys):
    """Lowering the ceiling under the held count is a refusal naming both
    numbers, not a silent over-subscription: the launcher would otherwise
    keep refusing against a ceiling the screen says has room."""
    add_front(env, "alpha")
    capsys.readouterr()
    capacity.grant(pool="muse", front="alpha", role="muse", job="job-one",
                   session="ses-one00001")
    capacity.grant(pool="muse", front="alpha", role="muse", job="job-two",
                   session="ses-two00001")
    assert capacity.held_by_front_role()[("alpha", "muse")] == 2
    lines = len(store.read_ledger(paths.front_record_path("alpha")))

    assert cli.main(["front", "allocate", "alpha", "muse", "1"]) == 1
    err = capsys.readouterr().err

    assert "2 held" in err
    assert "ceiling 1" in err
    assert len(store.read_ledger(paths.front_record_path("alpha"))) == lines
    assert capacity.ceiling("alpha", "muse") == 2


def test_allocate_refusals_name_every_violation_at_once(env, capsys):
    """An unknown front, a role no pool serves and a negative number join
    one refusal, and nothing is appended: the owner fixes everything in one
    round instead of one refusal per mistake."""
    assert cli.main(["front", "allocate", "ghost", "wizard", "-3"]) == 1
    err = capsys.readouterr().err

    assert "ghost" in err
    assert "wizard" in err
    assert "negative" in err
    assert err.count("refused:") == 1
    assert not paths.front_record_path("ghost").exists()


def test_allocate_is_the_foremans_verb_not_the_supervisors(
        env, capsys, monkeypatch):
    """A supervisor session is refused by role name; the foreman's session
    goes through. The owner calls it everywhere else in this file."""
    add_front(env, "alpha")
    capsys.readouterr()
    store.write_snapshot(paths.roster_path(), {"sessions": {
        "ses-sup0001": entities.Session(
            id="ses-sup0001", role="supervisor").to_dict(),
        "ses-for0001": entities.Session(
            id="ses-for0001", role="foreman").to_dict()}})

    monkeypatch.setenv(SESSION_ENV, "ses-sup0001")
    assert cli.main(["front", "allocate", "alpha", "muse", "4"]) == 1
    assert "may not call 'front allocate'" in capsys.readouterr().err

    monkeypatch.setenv(SESSION_ENV, "ses-for0001")
    assert cli.main(["front", "allocate", "alpha", "muse", "4"]) == 0
    capsys.readouterr()
    assert capacity.ceiling("alpha", "muse") == 4


def test_status_shows_a_changed_cap_with_no_collector_tick(env, capsys):
    """`foreman cap` takes effect on the screen at once.

    The collector ticks once with the cap at 1, so observed.json caches the
    old ceiling; the owner then raises it to 4 and the next `foreman status`
    — with no tick and no restart in between — already shows 1/4 while the
    collector's cached copy still says 1.
    """
    add_front(env, "alpha")
    assert cli.main(["cap", "muse", "1"]) == 0
    capsys.readouterr()
    capacity.grant(pool="muse", front="alpha", role="muse", job="job-held",
                   session="ses-held0001")
    safe_tick()

    assert cli.main(["status"]) == 0
    assert "  muse: 1/1 held" in capsys.readouterr().out.split(
        "Capacity:\n", 1)[1]

    assert cli.main(["cap", "muse", "4"]) == 0
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    block = capsys.readouterr().out.split("Capacity:\n", 1)[1]
    assert "  muse: 1/4 held" in block
    assert "  alpha muse: 1/2 held" in block
    observed = store.read_snapshot(paths.observed_path())
    assert observed["pools"]["muse"]["total"] == 1, \
        "the collector never re-ticked; the screen must not read this copy"


def test_a_tick_reads_the_config_fresh_each_time(env, capsys):
    """Two ticks with a `cap` between them write different totals.

    The daemon re-reads the configuration on every tick rather than holding
    the one it started with, so the totals each tick writes are governed by
    the caps in force now. A tick that reused a startup-held config would
    write 1 twice here.
    """
    assert cli.main(["cap", "muse", "1"]) == 0
    capsys.readouterr()
    tick()

    assert cli.main(["cap", "muse", "4"]) == 0
    capsys.readouterr()
    tick()

    assert store.read_snapshot(paths.observed_path())["pools"]["muse"][
        "total"] == 4
