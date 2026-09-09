"""`foreman attach <session>`: open a window on a session's turn log.

A headless session has no window to look at: one turn is one short
process, and between turns the session is a roster row with a log. Attach
opens a window on the launcher's workspace tailing that log — a look, not
a console — and closing it changes nothing: no roster write, no turn
ended, no ledger line. A `claude --resume` read-only console is
explicitly out of scope: this is a tail and stays one.

A session that never ran a turn has no log worth tailing, so attach
refuses it by name rather than opening an empty window.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path
from subprocess import DEVNULL
from typing import Any

from . import caller, cli, paths, store
from .caller import FOREMAN, MERGE_DESK, SUPERVISOR, Refusal
from .pools import _common

#: The window class the helper parks on the launcher's workspace, after
#: the supervisor's own `org.agent.foreman-<id>`: a different name so the
#: two rules never catch each other's windows.
ATTACH_SCRIPT_FILE = "attach.sh"


def attach_window_name(session_id: str) -> str:
    """The window class one attach opens: `org.agent.foreman-attach-<id>`."""
    return f"org.agent.foreman-attach-{session_id}"


def attach_script_inner(*, session_id: str, log_path: Path | str) -> str:
    """The attach window's body: the turn log, tailed, nothing else.

    The log path is absolute — a window the helper opens starts where the
    helper starts it, not in the session's directory — and quoted, so a
    state directory with a space in it still tails.
    """
    return (
        f"echo {shlex.quote('tailing turn log for ' + session_id + ': ' + str(log_path))}\n"
        f"echo {shlex.quote('close this window any time; it changes nothing')}\n"
        f"tail -n 200 -f {shlex.quote(str(log_path))}\n"
    )


def attach_outer_argv(session_id: str, script_path: Path | str) -> list[str]:
    """Place the window through the shipped helper; never reimplement it.

    No workspace travels: the helper parks the window on the workspace of
    the session operating it — the launcher's — which is where a closer
    look belongs. An explicit workspace would put the owner's look
    somewhere the owner is not.
    """
    return [_common.window_launcher_or_default(),
            f"foreman-attach-{session_id}", "bash", str(script_path)]


def write_attach_script(session_id: str) -> Path:
    """Write the window's script beside the session. No ledger, no roster.

    The script is a view, not a record: it lives next to `run.sh` the way
    every other per-session file does, and opening, closing or reopening
    it changes no byte the screen reads.
    """
    script = paths.session_dir(session_id) / ATTACH_SCRIPT_FILE
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/bash\n"
        + attach_script_inner(session_id=session_id,
                              log_path=paths.session_log_path(session_id)),
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def _default_spawn(argv: list[str]) -> Any:
    """Open the window and return at once: nothing here waits on it.

    The helper daemonizes the terminal itself, so unlike a launch there
    is no pid file to read back and no process to confirm — the window is
    open or the helper said why not.
    """
    return subprocess.Popen(argv, stdin=DEVNULL, stdout=DEVNULL,
                            stderr=DEVNULL, start_new_session=True,
                            close_fds=True)


def attach_main(target: str | None, spawn: Any = None) -> int:
    """Open a window tailing ``target``'s turn log. See the module docstring."""
    from . import headless as headless_module

    verb = "attach"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, FOREMAN, SUPERVISOR, MERGE_DESK,
                      violations=violations)
    name = (target or "").strip()
    record: dict | None = None
    if not name:
        violations.append("field 'session' is required")
    else:
        sessions = caller.read_roster().get("sessions", {})
        found = sessions.get(name) if isinstance(sessions, dict) else None
        record = found if isinstance(found, dict) else None
        if record is None:
            violations.append(f"unknown session '{name}'")
        elif not headless_module.read_turns(name):
            violations.append(
                f"session '{name}' never ran a turn; nothing to tail")
    if violations:
        return Refusal(violations).report()
    assert record is not None
    script = write_attach_script(name)
    argv = attach_outer_argv(name, script)
    run = spawn if spawn is not None else _default_spawn
    run(argv)
    log = paths.session_log_path(name)
    print(f"session: {name}")
    print(f"log: {log}")
    print(f"window: {attach_window_name(name)} "
          f"(the launcher's workspace; closing it changes nothing)")
    print(f"command: {_common.printable_command(argv, script.read_text(encoding='utf-8'))}")
    return 0


def add_attach_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("session", help="session whose turn log to tail")


@cli.subcommand("attach", help="Open a window tailing a session's turn log.")
def _attach_entry(args: argparse.Namespace) -> int:
    return attach_main(args.session)


_attach_entry.add_arguments = add_attach_arguments  # type: ignore[attr-defined]
