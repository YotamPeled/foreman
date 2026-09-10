"""Filesystem locations for Foreman state and config.

Every other module asks this one; nothing else builds a path by hand.
Layout follows docs/DESIGN.md section 6.
"""

from __future__ import annotations

import os
import shutil
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


def reservations_path() -> Path:
    """Append-only front reservations, folded last-wins by id.

    A front's team is held against the pool cap until the line carries
    ``released_at``. Readers fold; nothing rewrites a line already written.
    """
    return state_dir() / "reservations.jsonl"


def pools_path() -> Path:
    """Append-only pool state, folded last-wins by pool name.

    A record says a pool is out until a named reset (quota). Readers
    compare ``out_until`` to now; nothing rewrites the ledger to clear it.
    """
    return state_dir() / "pools.jsonl"


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


def clockwork_path() -> Path:
    """Append-only: one line per clockwork item run, by the collector."""
    return state_dir() / "clockwork.jsonl"


def clockwork_output_dir() -> Path:
    """Where a configured clockwork item's combined output is kept."""
    return state_dir() / "clockwork"


def panel_path() -> Path:
    """The panel's one summary file: every fact its eight blocks render.

    Written by the runtime (see :mod:`foreman.panel_feed`), read by the
    panel and by nothing else. It derives from the ledgers and holds no
    fact of its own, so deleting it costs one rewrite.
    """
    return state_dir() / "panel.json"


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


def front_findings_dir(name: str) -> Path:
    """Copied evidence files for findings on this front."""
    return front_dir(name) / "findings"


def front_measurements_path(name: str) -> Path:
    return front_dir(name) / "measurements.jsonl"


def front_map_path(name: str) -> Path:
    return front_dir(name) / "map.jsonl"


def front_milestones_path(name: str) -> Path:
    return front_dir(name) / "milestones.jsonl"


def front_tree_path(name: str) -> Path:
    return front_dir(name) / "tree.jsonl"


def front_flakes_path(name: str) -> Path:
    return front_dir(name) / "flakes.jsonl"


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


def session_turns_path(session_id: str) -> Path:
    """One headless turn's result line per record, appended turn by turn."""
    return session_dir(session_id) / "turns.jsonl"


def session_scratch_dir(session_id: str) -> Path:
    return session_dir(session_id) / "scratch"


def scratch_worktree_dir(kind: str, name: str) -> Path:
    """``<state>/worktrees/scratch/<kind>-<name>``, parents created.

    ``kind`` is ``verify`` or ``land``; ``name`` is the job or merge id.
    The leaf is left uncreated so ``git worktree add`` can own it.
    """
    path = state_dir() / "worktrees" / "scratch" / f"{kind}-{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def remove_scratch(path: Path | str) -> None:
    """Remove a scratch worktree directory, or do nothing if it is gone."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return


def merge_check_log_path(session_id: str, merge_id: str) -> Path:
    """The check's combined output for one landing, under the desk session."""
    return session_dir(session_id) / "merges" / f"{merge_id}-check.log"


def landing_lock_path(repo_key: str) -> Path:
    """Per-repository lock for a landing script, under the state directory."""
    return state_dir() / "land-locks" / repo_key


def landing_check_log_path(item_id: str) -> Path:
    """The check's combined output for one script landing."""
    return state_dir() / "landings" / f"{item_id}-check.log"


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
