"""`foreman migrate`: run packaged migrations once each, in lexical order.

Migrations live beside this module in ``migrations/<timestamp>.py``,
where the timestamp is the authoring commit's date, so lexical order is
chronological order. Each runs once: after an exit 0 the runner touches
a marker of that name under the state directory's ``migrations/``
markers, and the marker set is the version — there is no schema
integer.

A migration is a standalone script (standard library only, no Foreman
imports) taking ``--state-dir`` and ``--config-dir``. It runs with
stdin closed, so one that reads stdin cannot swallow anything queued
behind it. The failure split, per the Omarchy doctrine: a condition a
migration cannot repair prints a notice and exits 0, so nothing queued
behind it is blocked; only a missing permission exits non-zero, and
that migration stays pending for a re-run from a terminal that has it.
``--pending`` exits 0 when migrations are pending and 1 when none, so
it reads as a shell condition.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import caller, cli, paths
from .caller import Refusal
from .cli import subcommand

#: A migration that hangs the verb hangs the operator with it.
MIGRATION_TIMEOUT_SECONDS = 300


def packaged_dir() -> Path:
    """The shipped migrations directory."""
    return Path(__file__).with_name("migrations")


def markers_dir() -> Path:
    return paths.state_dir() / "migrations"


def available() -> list[Path]:
    """Every packaged migration, lexical (hence chronological) order."""
    try:
        entries = sorted(packaged_dir().iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [entry for entry in entries
            if entry.is_file() and entry.suffix == ".py"
            and not entry.name.startswith((".", "_"))]


def marked() -> set[str]:
    """Migration names already run (the marker set is the version)."""
    try:
        return {entry.name for entry in markers_dir().iterdir()}
    except OSError:
        return set()


def pending() -> list[Path]:
    """Packaged migrations with no marker yet, in run order."""
    done = marked()
    return [entry for entry in available() if entry.stem not in done]


def mark(path: Path) -> None:
    markers_dir().mkdir(parents=True, exist_ok=True)
    (markers_dir() / path.stem).touch()


def run_one(path: Path) -> tuple[int, str]:
    """Run one migration; return (exit code, its stdout).

    Stdout is the notice channel (an unrepairable condition prints
    there and exits 0); the runner relays it so every line is
    attributable to the migration that wrote it. Stderr stays
    inherited: a traceback reads best unbuffered.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(path),
             "--state-dir", str(paths.state_dir()),
             "--config-dir", str(paths.config_dir())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, text=True,
            timeout=MIGRATION_TIMEOUT_SECONDS,
        )
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        print(f"foreman: migration {path.name} timed out after "
              f"{MIGRATION_TIMEOUT_SECONDS}s; left pending")
        return 1, ""
    except OSError as exc:
        print(f"foreman: migration {path.name} did not start "
              f"({exc}); left pending")
        return 1, ""


def migrate_main(pending_only: bool = False) -> int:
    verb = "migrate"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, violations=violations)
    if violations:
        return Refusal(violations).report()
    outstanding = pending()
    if pending_only:
        if not outstanding:
            print("migrate: nothing pending")
            return 1
        for path in outstanding:
            print(path.stem)
        return 0
    if not outstanding:
        print("migrate: nothing pending")
        return 0
    failed = 0
    for path in outstanding:
        code, notices = run_one(path)
        if notices.strip():
            sys.stdout.write(notices if notices.endswith("\n")
                             else notices + "\n")
        if code == 0:
            mark(path)
            print(f"migrate: applied {path.stem}")
        else:
            print(f"foreman: migration {path.name} exited {code}; "
                  f"left pending")
            failed += 1
    return 1 if failed else 0


def add_migrate_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--pending", action="store_true",
                     help="list pending migrations: exit 0 when any are "
                          "pending, 1 when none")


@subcommand("migrate", help="Run pending state migrations, once each.")
def _migrate_entry(args: argparse.Namespace) -> int:
    return migrate_main(pending_only=args.pending)


_migrate_entry.add_arguments = add_migrate_arguments  # type: ignore[attr-defined]
