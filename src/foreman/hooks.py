"""Lifecycle hooks: the user's own executables, fired with the event as JSON.

Five events: ``on-launch``, ``on-return``, ``on-land``, ``on-freeze`` and
``on-alert``. Each is a directory under the user's config directory
(``hooks/<event>/``, see :mod:`foreman.paths`); every executable in it
runs with the event as one JSON object on stdin, in sorted name order.
A hook that fails prints and the run continues — one bad hook never
stops the swarm. ``*.sample`` files never run, so shipped examples stay
inert where they land.

Nothing runs a hook the user did not put there themselves: there are no
packaged hooks and no hooks from a repository, only the config
directory. Hooks are subprocesses, never imports: no file outside this
repository is ever executed as Python by this module.

``freeze`` and ``thaw`` live here because they are the ``on-freeze``
event's source: the frozen file's presence is the state (the launcher
refuses while it exists), and flipping it fires the hooks after the
write is durable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import caller, cli, paths, store
from .caller import Refusal
from .cli import subcommand

#: Every event with a directory. Firing an unknown event is a programmer
#: error, not a user hook, so :func:`fire` refuses it loudly.
HOOK_EVENTS = ("on-launch", "on-return", "on-land", "on-freeze", "on-alert")

#: A hook that hangs the verb it fires from hangs the swarm with it: a
#: slow notification is a failed notification, and the run continues.
HOOK_TIMEOUT_SECONDS = 60


def hooks_dir(event: str) -> Path:
    """The user's directory for ``event``'s hooks."""
    return paths.config_dir() / "hooks" / event


def list_hooks(event: str) -> list[Path]:
    """The executables that would fire for ``event``, in run order.

    Missing directory, unreadable directory and non-executables are all
    silence, not errors: an event with no hooks is the common case, and
    a README or a ``*.sample`` beside them must not spam the run.
    """
    try:
        entries = sorted(hooks_dir(event).iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    out = []
    for entry in entries:
        if entry.name.endswith(".sample") or entry.name.startswith("."):
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        if os.access(entry, os.X_OK):
            out.append(entry)
    return out


def fire(event: str, payload: dict | None = None) -> list[tuple[str, int]]:
    """Run ``event``'s hooks with the event as one JSON object on stdin.

    Returns [(hook name, return code)] with -1 for a hook that could
    not be started or ran past the timeout. A failing hook prints and
    the run continues; this function never raises for a hook's failure
    or for a missing hooks directory.
    """
    if event not in HOOK_EVENTS:
        raise ValueError(f"unknown hook event {event!r}")
    merged = {"event": event, "at": store.utcnow_iso()}
    merged.update(payload or {})
    body = json.dumps(merged) + "\n"
    results: list[tuple[str, int]] = []
    for path in list_hooks(event):
        try:
            proc = subprocess.run(
                [str(path)], input=body, text=True,
                timeout=HOOK_TIMEOUT_SECONDS,
            )
            code = proc.returncode
        except subprocess.TimeoutExpired:
            print(f"foreman: hook {event}/{path.name} timed out after "
                  f"{HOOK_TIMEOUT_SECONDS}s; continuing",
                  file=sys.stderr)
            results.append((path.name, -1))
            continue
        except OSError as exc:
            print(f"foreman: hook {event}/{path.name} did not start "
                  f"({exc}); continuing", file=sys.stderr)
            results.append((path.name, -1))
            continue
        if code != 0:
            print(f"foreman: hook {event}/{path.name} failed "
                  f"(exit {code}); continuing", file=sys.stderr)
        results.append((path.name, code))
    return results


# --------------------------------------------------------------------------
# `foreman hook install|list`
# --------------------------------------------------------------------------


def hook_install_main(event: str | None, source: str | None) -> int:
    verb = "hook install"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, violations=violations)
    name = (event or "").strip()
    if not name:
        violations.append("field 'event' is required "
                          f"(one of: {', '.join(HOOK_EVENTS)})")
    elif name not in HOOK_EVENTS:
        violations.append(f"unknown hook event '{name}' "
                          f"(events: {', '.join(HOOK_EVENTS)})")
    src = Path(source) if source else None
    if src is None or not str(source or "").strip():
        violations.append("field 'file' is required (the hook to install)")
    elif not src.is_file():
        violations.append(f"field 'file' is not a file (got '{source}')")
    dest = hooks_dir(name) / src.name if (name and src is not None) else None
    if dest is not None and dest.exists():
        violations.append(f"hook '{name}/{src.name}' already exists at "
                          f"{dest}; remove it first to replace it")
    if violations:
        return Refusal(violations).report()
    assert dest is not None and src is not None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        os.chmod(dest, 0o755)
    except OSError as exc:
        return Refusal(
            [f"cannot install hook '{name}/{src.name}' at {dest}: "
             f"{exc.strerror or exc}"]).report()
    print(f"{name}/{src.name} installed at {dest}")
    return 0


def hook_list_main() -> int:
    verb = "hook list"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, violations=violations)
    if violations:
        return Refusal(violations).report()
    for event in HOOK_EVENTS:
        hooks = list_hooks(event)
        if not hooks:
            print(f"{event}: (no hooks)")
        else:
            for path in hooks:
                print(f"{event}: {path.name}")
    return 0


def add_hook_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="hook_verb", required=True)
    install = verbs.add_parser("install", help="Install a hook executable.")
    install.add_argument("event", help="event to fire it on: "
                         + ", ".join(HOOK_EVENTS))
    install.add_argument("file", help="executable file to install")
    verbs.add_parser("list", help="Print the hooks that would fire.")


@subcommand("hook", help="Install or list lifecycle hooks.")
def _hook_entry(args: argparse.Namespace) -> int:
    if args.hook_verb == "install":
        return hook_install_main(args.event, args.file)
    if args.hook_verb == "list":
        return hook_list_main()
    raise AssertionError(f"unknown hook verb {args.hook_verb!r}")


_hook_entry.add_arguments = add_hook_arguments  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# `foreman freeze` / `foreman thaw`
# --------------------------------------------------------------------------


@subcommand("freeze", help="Freeze dispatch: refuse launches until thaw.")
def _freeze_entry(args: argparse.Namespace) -> int:
    verb = "freeze"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, violations=violations)
    if violations:
        return Refusal(violations).report()
    target = paths.frozen_path()
    if target.exists():
        return Refusal(
            [f"already frozen ({target} exists); "
             f"nothing new was frozen"]).report()
    who = caller.by_line(me)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"frozen {store.utcnow_iso()} by {who}\n",
                          encoding="utf-8")
    except OSError as exc:
        return Refusal([f"cannot freeze at {target}: "
                        f"{exc.strerror or exc}"]).report()
    store.mark_written()
    print(f"frozen ({target})")
    fire("on-freeze", {"frozen": True, "by": who})
    return 0


@subcommand("thaw", help="Thaw dispatch: allow launches again.")
def _thaw_entry(args: argparse.Namespace) -> int:
    verb = "thaw"
    me, violations = caller.resolve(verb)
    caller.check_role(me, verb, violations=violations)
    if violations:
        return Refusal(violations).report()
    target = paths.frozen_path()
    if not target.exists():
        return Refusal(["not frozen (nothing to thaw)"]).report()
    who = caller.by_line(me)
    try:
        target.unlink()
    except OSError as exc:
        return Refusal([f"cannot thaw ({target} stays): "
                        f"{exc.strerror or exc}"]).report()
    store.mark_written()
    print("thawed")
    fire("on-freeze", {"frozen": False, "by": who})
    return 0
