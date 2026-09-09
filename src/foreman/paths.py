"""Filesystem locations for Foreman state and config.

Every other module asks this one; nothing else builds a path by hand.
Layout follows docs/DESIGN.md section 6.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

STATE_ENV = "FOREMAN_STATE"
CONFIG_ENV = "FOREMAN_CONFIG"

STATE_HOME = Path.home() / ".local" / "state" / "foreman"
CONFIG_HOME = Path.home() / ".config" / "foreman"


def state_dir() -> Path:
    override = os.environ.get(STATE_ENV)
    return Path(override).expanduser() if override else STATE_HOME


def config_dir() -> Path:
    override = os.environ.get(CONFIG_ENV)
    return Path(override).expanduser() if override else CONFIG_HOME


def lock_path() -> Path:
    return state_dir() / "lock"


def frozen_path() -> Path:
    return state_dir() / "frozen"


def roster_path() -> Path:
    return state_dir() / "roster.json"


def observed_path() -> Path:
    return state_dir() / "observed.json"


def slots_path() -> Path:
    return state_dir() / "slots.jsonl"


def rulings_path() -> Path:
    return state_dir() / "rulings.jsonl"


def inbox_path() -> Path:
    return state_dir() / "inbox.jsonl"


def merges_path() -> Path:
    return state_dir() / "merges.jsonl"


def anomalies_path() -> Path:
    return state_dir() / "anomalies.jsonl"


def collector_path() -> Path:
    return state_dir() / "collector.json"


def events_path(day: str | None = None) -> Path:
    if day is None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state_dir() / "events" / f"{day}.jsonl"


def fronts_dir() -> Path:
    return state_dir() / "fronts"


def front_dir(name: str) -> Path:
    return fronts_dir() / name


def front_record_path(name: str) -> Path:
    """The front's own record. Append-only like every other ledger: a change
    appends a revised copy and readers fold last-wins, so `front add`,
    `front prefer` and `front done` never edit a byte already written."""
    return front_dir(name) / "front.jsonl"


def front_tasks_path(name: str) -> Path:
    return front_dir(name) / "tasks.jsonl"


def front_jobs_path(name: str) -> Path:
    return front_dir(name) / "jobs.jsonl"


def front_evidence_path(name: str) -> Path:
    return front_dir(name) / "evidence.jsonl"


def front_findings_path(name: str) -> Path:
    return front_dir(name) / "findings.jsonl"


def front_measurements_path(name: str) -> Path:
    return front_dir(name) / "measurements.jsonl"


def sessions_dir() -> Path:
    return state_dir() / "sessions"


def session_dir(session_id: str) -> Path:
    return sessions_dir() / session_id


def checkpoint_path(session_id: str) -> Path:
    return session_dir(session_id) / "checkpoint.json"


def session_log_path(session_id: str) -> Path:
    return session_dir(session_id) / "log"


def session_pid_path(session_id: str) -> Path:
    return session_dir(session_id) / "pid"


def session_verdict_path(session_id: str) -> Path:
    return session_dir(session_id) / "verdict.json"


def session_events_path(session_id: str) -> Path:
    return session_dir(session_id) / "events.jsonl"


def session_turn_path(session_id: str) -> Path:
    return session_dir(session_id) / "turn.json"


def session_scratch_dir(session_id: str) -> Path:
    return session_dir(session_id) / "scratch"


def config_file() -> Path:
    return config_dir() / "foreman.toml"


def config_front_dir(name: str) -> Path:
    return config_dir() / "fronts" / name


def brief_path(name: str) -> Path:
    return config_front_dir(name) / "brief.toml"


def plan_path(name: str) -> Path:
    return config_front_dir(name) / "plan.md"


def pool_dir(name: str) -> Path:
    return config_dir() / "pools" / name


def ensure_state_tree() -> Path:
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    fronts_dir().mkdir(exist_ok=True)
    sessions_dir().mkdir(exist_ok=True)
    (root / "events").mkdir(exist_ok=True)
    return root
