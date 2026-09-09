"""A pool that answered a quota refusal is out until the reset it named.

The break each test catches is in its docstring. Transcripts are the
observed Muse 429 shape (and Grok's JSON equivalent); clocks are injected;
nothing calls a vendor or touches the machine's systemd units or live
state directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman import capacity, paths, store
from foreman.collector import CollectorConfig, tick
from foreman.entities import Session
from foreman.pools import claude as claude_pool
from foreman.pools import grok as grok_pool
from foreman.pools import muse as muse_pool

#: The observed Muse 429 line, one line as it lands in the session log.
MUSE_429 = (
    "API error 429: Subscription quota exhausted. Your usage window resets "
    "at 2026-09-14T00:00:00Z. (rate_limit_error)\n"
)
RESET = "2026-09-14T00:00:00Z"
RESET_SHOWN = "2026-09-14 00:00Z"
BEFORE = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
AFTER = datetime(2026, 9, 14, 0, 0, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    return tmp_path


def muse_session(env: Path, sid: str, text: str) -> Session:
    log = paths.session_log_path(sid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    return Session(id=sid, role="muse", pool="muse",
                   model=muse_pool.MODEL, state="exited", log=str(log))


def grok_session(env: Path, sid: str, text: str) -> Session:
    log = paths.session_log_path(sid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    return Session(id=sid, role="grok", pool="grok",
                   model=grok_pool.MODEL, state="exited", log=str(log))


def write_out(pool: str = "muse", job: str = "job-quota01",
              session: str = "ses-quota001",
              reset: str = RESET) -> dict:
    return store.append_ledger(paths.pools_path(), {
        "id": pool, "pool": pool, "out_until": reset,
        "because": "quota",
        "detail": " ".join(MUSE_429.split()),
        "job": job, "session": session,
    })


# --------------------------------------------------------------------------
# The adapter says what it read
# --------------------------------------------------------------------------


def test_muse_refusal_reads_the_429_reset(env):
    """The observed Muse 429 line names a reset; missing that line, or
    naming a rate limit with no reset, must not put the pool out on a
    guess."""
    adapter = muse_pool.MuseAdapter()
    found = adapter.refusal(muse_session(env, "ses-ref429", MUSE_429))
    assert found == {
        "kind": "quota",
        "reset": RESET,
        "detail": " ".join(MUSE_429.split()),
    }

    silent = muse_session(
        env, "ses-refnone",
        '{"payload":{"event":{"kind":"model_completed"}}}\n')
    assert adapter.refusal(silent) is None

    no_reset = muse_session(
        env, "ses-refbare",
        "API error 429: rate_limit_error, try later\n")
    assert adapter.refusal(no_reset) is None

    missing = Session(id="ses-nolog", role="muse", pool="muse",
                      model=muse_pool.MODEL, state="exited")
    assert adapter.refusal(missing) is None


def test_grok_refusal_reads_the_same_shape_from_json(env):
    """Grok's JSON log carries the same refusal in a string field; a
    result with no reset is not a refusal."""
    adapter = grok_pool.GrokAdapter()
    blob = json.dumps({"error": MUSE_429.strip(), "usage": {
        "input_tokens": 1, "output_tokens": 0}}) + "\n"
    found = adapter.refusal(grok_session(env, "ses-grok429", blob))
    assert found is not None
    assert found["kind"] == "quota"
    assert found["reset"] == RESET

    clean = grok_session(env, "ses-grokclean", json.dumps({
        "text": "ok", "usage": {"input_tokens": 1, "output_tokens": 0}}) + "\n")
    assert adapter.refusal(clean) is None


def test_claude_keeps_the_default_even_with_a_429_in_the_log(env):
    """Claude does not read refusals; a 429 in its transcript is not a
    pool-out, because this pool keeps the default."""
    log = paths.session_log_path("ses-claude429")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(MUSE_429, encoding="utf-8")
    session = Session(id="ses-claude429", role="opus", pool="claude",
                      model="claude-opus-5", state="exited", log=str(log))
    assert claude_pool.ClaudeAdapter().refusal(session) is None


# --------------------------------------------------------------------------
# Who writes it
# --------------------------------------------------------------------------


def test_a_dead_worker_with_a_429_writes_one_pool_record(env):
    """One collector tick over a dead worker whose transcript holds the
    Muse 429 line writes one pools.jsonl record naming the pool and the
    reset. A second tick over the same job writes no second record —
    the ledger would otherwise grow without end for every finished
    quota death."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    pid = proc.pid
    proc.kill()
    proc.wait()

    sid = "ses-quota001"
    job = "job-quota01"
    log = paths.session_log_path(sid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(MUSE_429 + "### finished rc=1\n", encoding="utf-8")
    store.write_snapshot(paths.roster_path(), {"sessions": {
        sid: Session(
            id=sid, role="muse", pool="muse", model=muse_pool.MODEL,
            front="comp", job=job, pid=pid, pgid=pid,
            worktree="", log=str(log), timeout="20m",
            started_at=NOW.isoformat(), state="running",
        ).to_dict()}})
    store.append_ledger(paths.front_jobs_path("comp"), {
        "id": job, "task": "tas-1", "kind": "implement", "role": "muse",
        "state": "running", "session": sid, "started_at": NOW.isoformat()})

    tick(now=NOW, config=CollectorConfig(vendor_markers=("no-such-vendor",)))
    lines = store.read_ledger(paths.pools_path())
    assert len(lines) == 1
    record = lines[0]
    assert record["id"] == "muse"
    assert record["pool"] == "muse"
    assert record["out_until"] == RESET
    assert record["because"] == "quota"
    assert record["job"] == job
    assert record["session"] == sid

    tick(now=NOW, config=CollectorConfig(vendor_markers=("no-such-vendor",)))
    assert len(store.read_ledger(paths.pools_path())) == 1


# --------------------------------------------------------------------------
# What it changes
# --------------------------------------------------------------------------


def test_a_launch_onto_an_out_pool_is_refused_until_the_reset(env):
    """With the record in the ledger and now before the reset, a launch
    onto the pool is refused and the refusal names the pool and the
    reset. After the reset the same launch is admitted. A launch that
    is also over the cap is told both."""
    write_out()

    problems = capacity.launch_problems("grok", "muse", "runtime-v4",
                                        now=BEFORE)
    assert problems == [
        "role 'grok' on front 'runtime-v4': pool 'muse' is out until "
        f"{RESET_SHOWN} (quota)",
    ]

    assert capacity.launch_problems("grok", "muse", "runtime-v4",
                                    now=AFTER) == []

    from foreman import cli
    assert cli.main(["cap", "muse", "0"]) == 0
    both = capacity.launch_problems("muse", "muse", "alpha", now=BEFORE)
    assert "role 'muse' on front 'alpha': 0 held in pool 'muse', cap 0" in both
    assert ("role 'muse' on front 'alpha': pool 'muse' is out until "
            f"{RESET_SHOWN} (quota)") in both


def test_capacity_lines_print_out_until_the_reset_then_held(env):
    """Before the reset the pool row is the out line, not held/cap.
    After it, the same ledger still folds but the row is the ordinary
    held row — nothing rewrites the ledger to clear it."""
    write_out()
    observed = {"pools": {"muse": {"held": 0, "total": 5}}}

    before = capacity.capacity_lines(observed, now=BEFORE)
    assert f"  muse: out until {RESET_SHOWN} (quota)" in before
    assert "  muse: 0/5 held" not in before

    after = capacity.capacity_lines(observed, now=AFTER)
    assert f"  muse: out until {RESET_SHOWN} (quota)" not in after
    assert "  muse: 0/5 held" in after
    assert len(store.read_ledger(paths.pools_path())) == 1
