"""Rulings, inbox and checkpoint verbs, through the real CLI and ledgers.

Every test drives ``cli.main`` against a fresh ``FOREMAN_STATE`` directory
with a hand-built roster, then reads the state files back. No mocks: the
break each test catches is a wrong branch, a missing side effect, or a
missing refusal in the verbs themselves.
"""

from __future__ import annotations

import pytest

from foreman import cli, entities, paths, store
from foreman.caller import SESSION_ENV

SUP = "ses-sup0001"
WORKER = "ses-wrk0001"
FOREMAN_SES = "ses-for0001"
GHOST = "ses-ghost01"


def _session(sid, role, front=None):
    return entities.Session(
        id=sid, role=role, pool="opus", model="opus", front=front,
        pid=1000, launched_by="ses-root", started_at="2026-09-08T00:00:00+00:00",
        state="running",
    ).to_dict()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    store.write_snapshot(paths.roster_path(), {"sessions": {
        SUP: _session(SUP, "supervisor", "corpus"),
        WORKER: _session(WORKER, "worker"),
        FOREMAN_SES: _session(FOREMAN_SES, "foreman"),
    }})
    return tmp_path


def run(monkeypatch, argv, session=None):
    if session is None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_ENV, session)
    return cli.main(argv)


def test_unknown_session_refused_and_seen_by_collector(
    state, monkeypatch, capsys
):
    rc = run(monkeypatch, ["rule", "list"], session=GHOST)
    out, err = capsys.readouterr()
    assert rc == 1
    assert GHOST in err
    assert "unregistered writer" in err
    anomalies = store.read_ledger(paths.anomalies_path())
    assert anomalies
    assert anomalies[-1]["kind"] == "unregistered writer"
    assert anomalies[-1]["subject"] == GHOST
    assert "rule" in anomalies[-1]["detail"]


def test_owner_with_no_roster_is_never_refused_for_identity(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert run(monkeypatch, ["rule", "list"]) == 0
    assert capsys.readouterr().out == ""


def test_worker_refused_supervisor_verb_by_name(state, monkeypatch, capsys):
    rc = run(monkeypatch,
             ["checkpoint", "--doing", "building", "--next", "verifying"],
             session=WORKER)
    assert rc == 1
    err = capsys.readouterr().err
    assert "'worker'" in err
    assert "checkpoint" in err


def test_call_wrong_twice_names_both(state, monkeypatch, capsys):
    rc = run(monkeypatch, ["ask", "--kind", "bogus", "spend the budget?"],
             session=SUP)
    assert rc == 1
    err = capsys.readouterr().err
    assert "'kind'" in err
    assert "'recommendation'" in err
    assert store.read_ledger(paths.inbox_path()) == []


def test_ask_refuses_missing_recommendation(state, monkeypatch, capsys):
    rc = run(monkeypatch, ["ask", "--kind", "money", "spend $5?"], session=SUP)
    assert rc == 1
    assert "'recommendation'" in capsys.readouterr().err


def test_ask_and_inbox_list_oldest_first(state, monkeypatch, capsys):
    assert run(monkeypatch, ["ask", "--kind", "money", "--recommend", "yes",
                             "spend $5?"], session=SUP) == 0
    first = capsys.readouterr().out.strip()
    assert run(monkeypatch, ["ask", "--kind", "error", "--recommend", "retry",
                             "job died"], session=SUP) == 0
    second = capsys.readouterr().out.strip()
    assert run(monkeypatch, ["inbox"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith(first) and "spend $5?" in lines[0]
    assert lines[1].startswith(second) and "job died" in lines[1]
    assert "[money]" in lines[0] and "0s" in lines[0]


def test_answer_creates_ruling_in_same_call(state, monkeypatch, capsys):
    assert run(monkeypatch, ["ask", "--kind", "money", "--recommend", "yes",
                             "spend $5 on meters?"], session=SUP) == 0
    iid = capsys.readouterr().out.strip()
    rulings_before = store.read_ledger(paths.rulings_path())
    assert run(monkeypatch, ["answer", iid, "yes, spend it"], session=None) == 0
    capsys.readouterr()
    rulings_after = store.read_ledger(paths.rulings_path())
    assert len(rulings_after) == len(rulings_before) + 1
    ruling = entities.Ruling.from_dict(rulings_after[-1])
    assert ruling.scope == "corpus"
    assert ruling.text == "yes, spend it"
    assert ruling.source == "owner"
    items = store.read_ledger(paths.inbox_path())
    item = entities.InboxItem.from_dict(items[-1])
    assert item.id == iid
    assert item.answer == "yes, spend it"
    assert item.answered_by == "owner"
    assert item.answered_at is not None
    assert run(monkeypatch, ["inbox"]) == 0
    assert iid not in capsys.readouterr().out


def test_answer_twice_is_refused(state, monkeypatch, capsys):
    assert run(monkeypatch, ["ask", "--kind", "scope", "--recommend", "yes",
                             "widen the brief?"], session=SUP) == 0
    iid = capsys.readouterr().out.strip()
    assert run(monkeypatch, ["answer", iid, "yes"], session=None) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["answer", iid, "no"], session=None) == 1
    assert "already answered" in capsys.readouterr().err


def test_answer_to_unknown_id_names_it(state, monkeypatch, capsys):
    assert run(monkeypatch, ["answer", "inb-missing", "yes"]) == 1
    err = capsys.readouterr().err
    assert "unknown inbox item" in err
    assert "inb-missing" in err


def test_acks_accumulate_on_a_ruling(state, monkeypatch, capsys):
    assert run(monkeypatch, ["rule", "swarm", "Verify by re-running."]) == 0
    rid = capsys.readouterr().out.strip()
    assert run(monkeypatch, ["rule", "ack", rid], session=SUP) == 0
    assert run(monkeypatch, ["rule", "ack", rid], session=FOREMAN_SES) == 0
    capsys.readouterr()
    assert run(monkeypatch, ["rule", "list"]) == 0
    out = capsys.readouterr().out
    assert rid in out and SUP in out and FOREMAN_SES in out
    records = store.read_ledger(paths.rulings_path())
    assert records[0]["text"] == "Verify by re-running."
    folded = {}
    for row in records:
        folded[row["id"]] = row
    assert folded[rid]["acks"] == [SUP, FOREMAN_SES]


def test_rule_list_is_oldest_first(state, monkeypatch, capsys):
    assert run(monkeypatch, ["rule", "swarm", "First ruling."]) == 0
    first = capsys.readouterr().out.strip()
    assert run(monkeypatch, ["rule", "corpus", "Second ruling."]) == 0
    second = capsys.readouterr().out.strip()
    assert run(monkeypatch, ["rule", "list"]) == 0
    out = capsys.readouterr().out
    assert out.index(first) < out.index(second)
    assert "[corpus] Second ruling." in out


def test_checkpoint_moves_roster_last_declared_write(
    state, monkeypatch, capsys
):
    before = store.read_snapshot(paths.roster_path())["sessions"][SUP][
        "last_declared_at"]
    assert before is None
    rc = run(monkeypatch, ["checkpoint", "--doing", "splitting the task",
                           "--next", "dispatching jobs",
                           "--held", "tas-1", "--waiting", "brief fix"],
             session=SUP)
    assert rc == 0
    capsys.readouterr()
    checkpoint = store.read_snapshot(paths.checkpoint_path(SUP))
    assert checkpoint["doing"] == "splitting the task"
    assert checkpoint["next"] == "dispatching jobs"
    assert checkpoint["held"] == ["tas-1"]
    assert checkpoint["questions"] == ["brief fix"]
    after = store.read_snapshot(paths.roster_path())["sessions"][SUP][
        "last_declared_at"]
    assert after is not None and after != before


def test_self_contained_warning_does_not_block_write(
    state, monkeypatch, capsys
):
    assert run(monkeypatch, ["rule", "swarm", ""]) == 0
    out, err = capsys.readouterr()
    assert "warning" in err
    records = store.read_ledger(paths.rulings_path())
    assert entities.Ruling.from_dict(records[-1]).text == ""
    assert run(monkeypatch, ["checkpoint", "--doing", "ses-abcdefg",
                             "--next", "dispatching"], session=SUP) == 0
    out, err = capsys.readouterr()
    assert "warning" in err
    assert "nothing but an id" in err
    assert store.read_snapshot(paths.checkpoint_path(SUP))["doing"] == (
        "ses-abcdefg"
    )
