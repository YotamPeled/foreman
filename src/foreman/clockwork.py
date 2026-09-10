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
import re
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
