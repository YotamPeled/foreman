"""Only a vendor error event can mark a pool out.

A rate-limit string inside a tool_call write, a thought, or a text
record is the worker quoting a proof, not the vendor refusing. Tests
that would stay green without the work are not in this file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from foreman import capacity, paths, status, store
from foreman.collector import CollectorConfig, tick
from foreman.entities import Session
from foreman.pools import claude as claude_pool
from foreman.pools import codex as codex_pool
from foreman.pools import grok as grok_pool
from foreman.pools import muse as muse_pool
from foreman.pools._common import parse_quota_refusal


REFUSAL = (
    "API error 429: Subscription quota exhausted. Your usage window resets "
    "at 2026-09-14T00:00:00Z. (rate_limit_error)"
)
RESET = "2026-09-14T00:00:00Z"


def _muse_event(kind: str, record: dict) -> dict:
    return {
        "schema_version": 1,
        "id": "x",
        "stream": {"kind": "session", "id": "s"},
        "sequence": 1,
        "recorded_at": 1,
        "record_type": "event",
        "durability": "durable",
        "payload_type": "runtime.session",
        "payload": {"kind": "run", "run_id": "r",
                    "event": {"kind": kind, **record}},
    }


def _write_call() -> dict:
    return {
        "type": "tool_call",
        "name": "Write",
        "arguments": {"path": "proof.py", "content": REFUSAL},
    }


POOLS = {
    "grok": {
        "adapter": grok_pool.GrokAdapter,
        "model": grok_pool.MODEL,
        "role": "grok",
        "error": {"type": "error", "error": REFUSAL},
        "tool_call": _write_call(),
        "thought": {"type": "thought", "text": REFUSAL},
        "error_type": "error",
    },
    "claude": {
        "adapter": claude_pool.ClaudeAdapter,
        "model": claude_pool.MODEL,
        "role": "opus",
        "error": {"type": "error", "error": REFUSAL},
        "tool_call": _write_call(),
        "thought": {"type": "thought", "text": REFUSAL},
        "error_type": "error",
    },
    "codex": {
        "adapter": codex_pool.CodexAdapter,
        "model": codex_pool.MODEL,
        "role": "astra",
        "error": {"type": "error", "error": REFUSAL},
        "tool_call": _write_call(),
        "thought": {"type": "thought", "text": REFUSAL},
        "error_type": "error",
    },
    "muse": {
        "adapter": muse_pool.MuseAdapter,
        "model": muse_pool.MODEL,
        "role": "muse",
        "error": _muse_event("error", {"message": REFUSAL}),
        "tool_call": _muse_event("tool_call", {
            "name": "Write",
            "arguments": {"path": "proof.py", "content": REFUSAL},
        }),
        "thought": _muse_event("thought", {"text": REFUSAL}),
        "error_type": "error",
    },
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    return tmp_path


def session_with(pool: str, sid: str, text: str) -> Session:
    spec = POOLS[pool]
    log = paths.session_log_path(sid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    return Session(id=sid, role=spec["role"], pool=pool,
                   model=spec["model"], state="exited", log=str(log))


def jsonl(*records: dict, finish: int | None = 1) -> str:
    lines = [json.dumps(record) for record in records]
    if finish is not None:
        lines.append(f"### finished rc={finish}")
    return "\n".join(lines) + "\n"


def test_parse_quota_refusal_keeps_its_contract():
    """The text parser still answers for the text it is given, whether
    or not a caller later walks records around it."""
    found = parse_quota_refusal(REFUSAL)
    assert found == {
        "kind": "quota",
        "reset": RESET,
        "detail": REFUSAL,
    }
    assert parse_quota_refusal("rate limit, try later") is None
    assert parse_quota_refusal("") is None


@pytest.mark.parametrize("pool", list(POOLS))
def test_tool_call_write_payload_is_not_a_refusal(env, pool):
    """The finding: a worker wrote a fake 429 into a file and the
    reader regex-searched the tool_call that echoed it."""
    spec = POOLS[pool]
    adapter = spec["adapter"]()
    text = jsonl({"type": "update", "text": "working"}, spec["tool_call"])
    found = adapter.refusal(session_with(pool, f"ses-{pool}-tool", text))
    assert found is None, pool


@pytest.mark.parametrize("pool", list(POOLS))
def test_thought_quoting_a_refusal_is_not_a_refusal(env, pool):
    spec = POOLS[pool]
    adapter = spec["adapter"]()
    found = adapter.refusal(session_with(
        pool, f"ses-{pool}-thought", jsonl(spec["thought"])))
    assert found is None, pool


@pytest.mark.parametrize("pool", list(POOLS))
def test_text_and_user_records_are_not_searched(env, pool):
    spec = POOLS[pool]
    adapter = spec["adapter"]()
    text = jsonl(
        {"type": "text", "text": REFUSAL},
        {"type": "user", "message": REFUSAL},
        {"type": "tool_result", "content": REFUSAL},
    )
    found = adapter.refusal(session_with(pool, f"ses-{pool}-quote", text))
    assert found is None, pool


@pytest.mark.parametrize("pool", list(POOLS))
def test_vendor_error_record_is_a_refusal(env, pool):
    spec = POOLS[pool]
    adapter = spec["adapter"]()
    text = jsonl(
        {"type": "update", "text": "working"},
        spec["tool_call"],
        spec["error"],
    )
    found = adapter.refusal(session_with(pool, f"ses-{pool}-err", text))
    assert found is not None, pool
    assert found["kind"] == "quota"
    assert found["reset"] == RESET
    assert found["record_type"] == spec["error_type"]
    assert found["line_no"] == 3


@pytest.mark.parametrize("pool", list(POOLS))
def test_plain_log_with_a_real_refusal_still_yields_it(env, pool):
    spec = POOLS[pool]
    adapter = spec["adapter"]()
    text = REFUSAL + "\n### finished rc=1\n"
    found = adapter.refusal(session_with(pool, f"ses-{pool}-plain", text))
    assert found is not None, pool
    assert found["kind"] == "quota"
    assert found["reset"] == RESET
    assert found["record_type"] == "log"
    assert found["line_no"] == 1


def test_grok_result_without_is_error_is_not_a_refusal(env):
    """A streaming result that merely mentions an error string is not
    the vendor's is_error terminal record."""
    adapter = grok_pool.GrokAdapter()
    text = jsonl({"type": "result", "error": REFUSAL})
    found = adapter.refusal(session_with("grok", "ses-grok-res", text))
    assert found is None


def test_grok_result_with_is_error_is_a_refusal(env):
    adapter = grok_pool.GrokAdapter()
    text = jsonl({"type": "result", "is_error": True, "error": REFUSAL})
    found = adapter.refusal(session_with("grok", "ses-grok-iserr", text))
    assert found is not None
    assert found["kind"] == "quota"
    assert found["record_type"] == "result"
    assert found["line_no"] == 1


def test_claude_result_with_is_error_is_a_refusal(env):
    adapter = claude_pool.ClaudeAdapter()
    text = jsonl({"type": "result", "is_error": True, "result": REFUSAL})
    found = adapter.refusal(session_with("claude", "ses-claude-iserr", text))
    assert found is not None
    assert found["kind"] == "quota"
    assert found["record_type"] == "result"


def test_codex_turn_failed_is_a_refusal(env):
    adapter = codex_pool.CodexAdapter()
    text = jsonl({"type": "turn.failed", "error": REFUSAL})
    found = adapter.refusal(session_with("codex", "ses-codex-turn", text))
    assert found is not None
    assert found["kind"] == "quota"
    assert found["record_type"] == "turn.failed"
    assert found["line_no"] == 1


NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
RESET_SHOWN = "2026-09-14 00:00Z"


def _dead_pid() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    pid = proc.pid
    proc.kill()
    proc.wait()
    return pid


def _tick_dead_worker(pool: str, sid: str, job: str, text: str) -> None:
    spec = POOLS[pool]
    log = paths.session_log_path(sid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    pid = _dead_pid()
    store.write_snapshot(paths.roster_path(), {"sessions": {
        sid: Session(
            id=sid, role=spec["role"], pool=pool, model=spec["model"],
            front="comp", job=job, pid=pid, pgid=pid,
            worktree="", log=str(log), timeout="20m",
            started_at=NOW.isoformat(), state="running",
        ).to_dict()}})
    store.append_ledger(paths.front_jobs_path("comp"), {
        "id": job, "task": "tas-1", "kind": "implement",
        "role": spec["role"], "state": "running", "session": sid,
        "started_at": NOW.isoformat()})
    tick(now=NOW, config=CollectorConfig(vendor_markers=("no-such-vendor",)))


@pytest.mark.parametrize("pool", list(POOLS))
def test_tick_does_not_mark_a_pool_out_from_a_tool_call(env, pool):
    """A tick over a dead worker whose only 429 sits in a tool_call
    write payload must leave pools.jsonl empty."""
    text = jsonl({"type": "update", "text": "working"},
                 POOLS[pool]["tool_call"])
    _tick_dead_worker(pool, f"ses-{pool}-ticktool", f"job-{pool}-tt", text)
    assert store.read_ledger(paths.pools_path()) == []


@pytest.mark.parametrize("pool", list(POOLS))
def test_tick_marks_the_pool_from_a_vendor_error_event(env, pool):
    """The same 429 inside a vendor error record marks the pool, and
    the mark names the record type and line so a person can check it."""
    spec = POOLS[pool]
    text = jsonl(
        {"type": "update", "text": "working"},
        spec["tool_call"],
        spec["error"],
    )
    sid = f"ses-{pool}-tickerr"
    job = f"job-{pool}-te"
    _tick_dead_worker(pool, sid, job, text)
    lines = store.read_ledger(paths.pools_path())
    assert len(lines) == 1, pool
    record = lines[0]
    assert record["id"] == pool
    assert record["pool"] == pool
    assert record["out_until"] == RESET
    assert record["because"] == "quota"
    assert record["job"] == job
    assert record["session"] == sid
    assert record["record_type"] == spec["error_type"]
    assert record["line_no"] == 3


def test_status_prints_the_record_type_and_line(env):
    """status and capacity both print ``out until ... (quota, error
    line N)`` when the mark names its source. An older record without
    those fields still prints ``(quota)``."""
    store.append_ledger(paths.pools_path(), {
        "id": "grok", "pool": "grok", "out_until": RESET,
        "because": "quota", "record_type": "error", "line_no": 3,
    })
    sourced = f"  grok: out until {RESET_SHOWN} (quota, error line 3)"
    lines = capacity.capacity_lines({}, now=NOW)
    assert sourced in lines
    screen = status.render(now=NOW)
    assert sourced in screen
    problems = capacity.launch_problems("grok", "grok", "alpha", now=NOW)
    assert problems == [
        "role 'grok' on front 'alpha': pool 'grok' is out until "
        f"{RESET_SHOWN} (quota, error line 3)",
    ]
    assert capacity.allocation_out("grok", now=NOW) == (
        f"out until {RESET_SHOWN} (quota, error line 3)"
    )

    store.append_ledger(paths.pools_path(), {
        "id": "muse", "pool": "muse", "out_until": RESET,
        "because": "quota",
    })
    plain = f"  muse: out until {RESET_SHOWN} (quota)"
    after = capacity.capacity_lines({}, now=NOW)
    assert plain in after
    assert f"  muse: out until {RESET_SHOWN} (quota, " not in "\n".join(after)
