"""The collector's clockwork: the work the swarm does on a clock.

Decision 9: the foreman's queue also relaunches a dead supervisor,
re-enables a pool at its reset time, cleans worktrees and dead sessions,
and runs the owner's nightly, merge-gate, report and upgrade commands.
The collector runs every item from its tick and records each run as one
line of ``<state>/clockwork.jsonl``: the item, when, the command, its
exit, how long it took, where its output is, and ``by = collector``.

The configured items come from ``[clockwork]`` in ``foreman.toml``::

    [clockwork]
    nightly = "<cmd>"
    nightly_every = "24h"      # or nightly_at = "03:00" (UTC)
    merge_gate = "<cmd>"
    merge_gate_every = "1h"
    clean_after = "24h"

An item with no command, or a command with no schedule, is skipped: a
schedule is never invented. The content of each command is the owner's.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime

from . import caller, paths, store
from .caller import Refusal
from .cli import subcommand
from .collector import COLLECTOR_SUBJECT, _parse_time, parse_duration

#: The items the collector runs each tick with no configuration.
BUILTIN_ITEMS = ("relaunch", "pool-reset", "clean", "dead-sessions")
#: The items the owner configures, in the order ``list`` prints them.
CONFIGURED_ITEMS = ("nightly", "merge_gate", "owner_report", "upgrade")

DEFAULT_CLEAN_AFTER_SECONDS = 24 * 60 * 60
DEFAULT_CLEAN_AFTER = "24h"

_AT = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Item:
    """One configured item: its command and exactly one schedule."""

    name: str
    command: str
    every: float | None = None
    every_text: str = ""
    at: tuple[int, int] | None = None
    at_text: str = ""

    @property
    def scheduled(self) -> bool:
        return self.every is not None or self.at is not None

    def schedule(self) -> str:
        if self.every is not None:
            return f"every {self.every_text}"
        if self.at is not None:
            return f"at {self.at_text} UTC"
        return "no schedule"


@dataclass(frozen=True)
class ClockworkConfig:
    items: dict[str, Item] = field(default_factory=dict)
    clean_after: float = DEFAULT_CLEAN_AFTER_SECONDS
    clean_after_text: str = DEFAULT_CLEAN_AFTER


def load_config(path=None) -> ClockworkConfig:
    """``[clockwork]`` from ``foreman.toml``; anything misshapen is skipped.

    A daemon must not die on its config, and an entry it cannot read is
    an entry the owner did not write: it is left out, never guessed.
    """
    try:
        with open(path or paths.config_file(), "rb") as handle:
            raw = tomllib.load(handle)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError, ValueError):
        return ClockworkConfig()
    table = raw.get("clockwork") if isinstance(raw, dict) else None
    if not isinstance(table, dict):
        return ClockworkConfig()
    items: dict[str, Item] = {}
    for name in CONFIGURED_ITEMS:
        command = table.get(name)
        if not isinstance(command, str) or not command.strip():
            continue
        every_text = table.get(f"{name}_every")
        every = parse_duration(every_text)
        at_text = table.get(f"{name}_at")
        match = _AT.match(at_text) if isinstance(at_text, str) else None
        if every is not None and every > 0:
            items[name] = Item(name, command.strip(), every=every,
                               every_text=str(every_text))
        elif match:
            items[name] = Item(name, command.strip(),
                               at=(int(match[1]), int(match[2])),
                               at_text=str(at_text))
        else:
            items[name] = Item(name, command.strip())
    clean_text = table.get("clean_after")
    clean = parse_duration(clean_text)
    if clean is not None and clean > 0:
        return ClockworkConfig(items, clean, str(clean_text))
    return ClockworkConfig(items)


def record_run(item: str, command: str, exit_code: int | None,
               seconds: float, output_ref: str = "", *,
               at: str | None = None, **extra) -> dict:
    """Append one clockwork line: an item ran, with this exit."""
    line = {"item": item, "at": at or store.utcnow_iso(),
            "command": command, "exit": exit_code,
            "seconds": round(float(seconds), 3),
            "output_ref": output_ref, "by": COLLECTOR_SUBJECT}
    line.update(extra)
    return store.append_ledger(paths.clockwork_path(), line)


def last_runs() -> dict[str, dict]:
    """The latest clockwork line per item."""
    try:
        records = store.read_ledger(paths.clockwork_path())
    except OSError:
        return {}
    latest: dict[str, dict] = {}
    for record in records:
        name = record.get("item") if isinstance(record, dict) else None
        if isinstance(name, str) and name:
            latest[name] = record
    return latest


def _pool_reset(moment: datetime, now_iso: str) -> int:
    """A pool whose ``out_until`` has passed gets one line clearing it.

    Folded last-wins, the clearing line carries ``out_until null``, so
    the next tick finds nothing past its reset and appends nothing.
    """
    from . import capacity

    reset = 0
    for record in capacity.folded_pools():
        pool = record.get("id")
        until = capacity._parse_iso(record.get("out_until"))
        if not isinstance(pool, str) or not pool or until is None \
                or moment < until:
            continue
        store.append_ledger(paths.pools_path(), {
            "id": pool, "pool": pool, "out_until": None,
            "because": "reset reached",
            "detail": f"out until {record.get('out_until')} passed",
            "at": now_iso, "by": COLLECTOR_SUBJECT,
        })
        record_run("pool-reset", f"pool {pool} out_until null", 0, 0.0,
                   at=now_iso, pool=pool)
        reset += 1
    return reset


def _git(worktree: str, *args: str) -> str | None:
    import subprocess

    try:
        proc = subprocess.run(["git", "-C", worktree, *args],
                              capture_output=True, text=True, timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def remove_session_files(sid: str, record: dict) -> str | None:
    """The one disk door: remove a session's worktree and scratch.

    None when done, else why not. Only a linked worktree is removed, and
    through the launcher's own undo with its branch kept: the branch may
    hold work not yet landed, and a main checkout is never a session's to
    delete. Tests replace this function.
    """
    import shutil

    from . import launch as launch_module

    problem = None
    worktree = str(record.get("worktree") or "")
    if worktree and os.path.isdir(worktree):
        common = _git(worktree, "rev-parse", "--path-format=absolute",
                      "--git-common-dir")
        own = _git(worktree, "rev-parse", "--path-format=absolute",
                   "--git-dir")
        if not common or not own:
            problem = f"{worktree} is not a git worktree"
        elif os.path.realpath(common) == os.path.realpath(own):
            problem = f"{worktree} is a main checkout, not a linked worktree"
        else:
            repo = os.path.dirname(common) \
                if os.path.basename(common) == ".git" else common
            launch_module.remove_launch_worktree(
                repo, str(record.get("branch") or ""), worktree,
                delete_branch=False)
            if os.path.isdir(worktree):
                problem = f"git worktree remove left {worktree} in place"
    shutil.rmtree(paths.session_scratch_dir(sid), ignore_errors=True)
    return problem


def _sessions_tick(moment: datetime, now_iso: str,
                   config: ClockworkConfig) -> None:
    """``clean`` and ``dead-sessions``: the roster's exited, tidied.

    A session the roster calls exited or killed gets ``exited_at`` the
    first tick that sees it so. Past ``clean_after`` from then, a
    worker's worktree and scratch go through :func:`remove_session_files`
    (one ``clean`` line per tick naming the count) and every such entry
    is marked ``history`` (one ``dead-sessions`` line), so neither is
    looked at again.
    """
    from .collector import TERMINAL_SESSION_STATES, WORKER_EXEMPT_ROLES

    roster = store.read_snapshot(paths.roster_path(), default=None)
    sessions = roster.get("sessions") if isinstance(roster, dict) else None
    if not isinstance(sessions, dict):
        return
    stamp: list[str] = []
    old: list[str] = []
    for sid, record in sessions.items():
        if not isinstance(record, dict) or \
                record.get("state") not in TERMINAL_SESSION_STATES:
            continue
        exited = _parse_time(record.get("exited_at"))
        if exited is None:
            stamp.append(sid)
        elif (moment - exited).total_seconds() > config.clean_after:
            old.append(sid)
    cleaned, failed = [], []
    for sid in old:
        record = sessions[sid]
        if (record.get("role") or "") in WORKER_EXEMPT_ROLES:
            continue
        worktree = str(record.get("worktree") or "")
        if not (worktree and os.path.isdir(worktree)) and \
                not paths.session_scratch_dir(sid).is_dir():
            continue
        try:
            problem = remove_session_files(sid, record)
        except Exception as exc:  # noqa: BLE001 - a failed removal is a
            problem = f"{type(exc).__name__}: {exc}"  # line, not a dead tick
        (failed if problem else cleaned).append((sid, problem))
    if cleaned or failed:
        record_run(
            "clean",
            f"remove {len(cleaned) + len(failed)} worktree(s) exited over "
            f"{config.clean_after_text}", 1 if failed else 0, 0.0,
            at=now_iso, count=len(cleaned),
            sessions=[sid for sid, _ in cleaned],
            failed={sid: problem for sid, problem in failed})
    if not stamp and not old:
        return

    def apply(current):
        entries = current.get("sessions") if isinstance(current, dict) \
            else None
        if not isinstance(entries, dict):
            return current
        for sid in stamp:
            entry = entries.get(sid)
            if isinstance(entry, dict) and "exited_at" not in entry:
                entry["exited_at"] = now_iso
        for sid in old:
            entry = entries.get(sid)
            if isinstance(entry, dict) and \
                    entry.get("state") in TERMINAL_SESSION_STATES:
                entry["state"] = "history"
                entry["history_at"] = now_iso
        return current

    store.update_snapshot(paths.roster_path(), apply,
                          default={"sessions": {}})
    if old:
        record_run("dead-sessions",
                   f"mark {len(old)} session(s) history", 0, 0.0,
                   at=now_iso, count=len(old), sessions=old)


def tick(moment: datetime, now_iso: str, cstate: dict, note_open,
         open_now: dict, config: ClockworkConfig | None = None) -> None:
    """One pass of every item. Called by the collector's tick, under its
    lock, after the roster write; nothing here raises into the tick."""
    config = config or load_config()
    for step in (lambda: _pool_reset(moment, now_iso),
                 lambda: _sessions_tick(moment, now_iso, config)):
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - the daemon stays up
            print(f"foreman clockwork: {type(exc).__name__}: {exc}",
                  file=sys.stderr)


def _last_text(record: dict | None) -> str:
    if record is None:
        return "never run"
    moment = _parse_time(record.get("at"))
    when = moment.strftime("%Y-%m-%d %H:%MZ") if isinstance(
        moment, datetime) else "?"
    code = record.get("exit")
    return f"last {when} exit {code if code is not None else '?'}"


def list_lines(config: ClockworkConfig | None = None) -> list[str]:
    """One line per item: name, schedule, last run and its exit."""
    config = config or load_config()
    latest = last_runs()
    rows: list[tuple[str, str, str]] = []
    for name in BUILTIN_ITEMS:
        schedule = "every tick"
        if name in ("clean", "dead-sessions"):
            schedule = f"every tick, after {config.clean_after_text}"
        rows.append((name, schedule, _last_text(latest.get(name))))
    for name in CONFIGURED_ITEMS:
        item = config.items.get(name)
        if item is None:
            continue
        rows.append((name, item.schedule(), _last_text(latest.get(name))))
    width = max(len(name) for name, _, _ in rows)
    sched = max(len(schedule) for _, schedule, _ in rows)
    return [f"{name.ljust(width)}  {schedule.ljust(sched)}  {last}"
            for name, schedule, last in rows]


def add_clockwork_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("list",),
                        help="print each item, its schedule, the last run "
                             "and its exit")


@subcommand("clockwork", help="The collector's clock: items and last runs.")
def _clockwork_entry(args: argparse.Namespace) -> int:
    me, violations = caller.resolve("clockwork")
    caller.check_role(me, "clockwork", violations=violations)
    if violations:
        return Refusal(violations).report()
    for line in list_lines():
        print(line)
    return 0


_clockwork_entry.add_arguments = add_clockwork_arguments  # type: ignore[attr-defined]
