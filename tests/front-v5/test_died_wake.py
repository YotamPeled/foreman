"""A job that dies wakes its launcher, and a refused wake never aborts the
transition that caused it (the collector's tick goes on)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foreman import entities, paths, store
from foreman import wake as wake_module
from foreman.caller import SESSION_ENV
from foreman.collector import _mark_job
from foreman.entities import Session

SUP = "ses-sup0001"
WORKER = "ses-wrk0001"
JOB = "job-0001"
FRONT = "alpha"


def iso(moment: datetime) -> str:
    return moment.isoformat()


def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    return tmp_path


def session(sid: str, role: str, **fields) -> dict:
    base = {
        "id": sid, "role": role, "pool": "fake", "model": "fake-test-model",
        "front": FRONT, "job": None, "pid": None, "pgid": None,
        "worktree": "", "log": "", "timeout": "", "launched_by": None,
        "started_at": iso(now()), "last_declared_at": None,
        "last_observed_at": None, "cpu_s": 0.0, "state": "running",
    }
    base.update(fields)
    return Session.from_dict(base).to_dict()


def running_job_launched_by_supervisor() -> None:
    store.write_snapshot(paths.roster_path(), {"sessions": {
        SUP: session(SUP, "supervisor"),
        WORKER: session(WORKER, "muse", job=JOB, launched_by=SUP)}})
    store.append_ledger(paths.front_jobs_path(FRONT), {
        "id": JOB, "task": "tas-0001", "kind": "implement", "role": "muse",
        "state": "running", "started_at": iso(now() - timedelta(minutes=5))})


def job_state() -> str:
    lines = [r for r in store.read_ledger(paths.front_jobs_path(FRONT))
             if r.get("id") == JOB]
    return lines[-1]["state"]


@pytest.mark.xfail(strict=True, reason="job-7.3f builds this; its worker removes this marker")
def test_died_job_wakes_its_launcher_with_a_listed_reason(env):
    running_job_launched_by_supervisor()
    _mark_job(FRONT, JOB, "died", iso(now()))
    assert job_state() == "died"
    events = wake_module.read_events(SUP)
    assert len(events) == 1
    assert events[0]["reason"] in entities.WAKE_REASONS
    assert events[0]["job"] == JOB


@pytest.mark.xfail(strict=True, reason="job-7.3f builds this; its worker removes this marker")
def test_refused_wake_leaves_the_transition_standing(env):
    running_job_launched_by_supervisor()
    _mark_job(FRONT, JOB, "failed", iso(now()),
              reason="a reason no vocabulary lists")
    assert job_state() == "failed"
    assert wake_module.read_events(SUP) == []
