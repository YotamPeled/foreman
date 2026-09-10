"""`foreman start --model M --effort E [--dry-run]`: collector and foreman.

Decision 2: one verb brings up the swarm. Two steps, each one line:

1. the collector unit. Absent: write it from ``render_unit`` into the
   user unit directory, ``daemon-reload`` and ``enable --now``. Present
   and active: left alone, never restarted (``collector restart`` stays
   the one door for that). Present and inactive: started.
2. the foreman, through the same path as ``foreman launch foreman``
   with the model and effort named here.

A live foreman refuses the whole verb before either step, naming it: a
start that installed a collector and then found the swarm held has done
half a thing. The owner runs this from a terminal; every session role
is refused. Every systemd call goes through ``collector.systemctl``.
Under ``--dry-run`` each step prints ``would <step>`` and nothing is
written or spawned. Exit 0 only when both steps succeeded or were dry.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys

from . import caller, collector
from . import launch as launch_module
from .caller import Refusal
from .cli import subcommand
from .pools._common import LAUNCH_EFFORTS

UNIT = collector.COLLECTOR_UNIT


def add_start_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", required=True,
                        help="the model the foreman runs")
    parser.add_argument("--effort", required=True, choices=LAUNCH_EFFORTS,
                        help="the foreman's effort: "
                             + ", ".join(LAUNCH_EFFORTS))
    parser.add_argument("--repo", default=None,
                        help="the checkout the foreman works in "
                             "(default: the current directory)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what each step would do; write and "
                             "start nothing")


def _collector_step(dry_run: bool) -> int:
    """Install, start or leave the collector unit; one line either way."""
    path = collector.unit_path()
    state = collector.unit_state()
    if state == "active":
        print("would leave the collector running (left alone)" if dry_run
              else "collector: running (left alone)")
        return 0
    if state == "inactive":
        if dry_run:
            print(f"would start {UNIT}")
            return 0
        code, said = collector.systemctl("start", UNIT)
        if code != 0:
            print(f"collector: start {UNIT} failed: {said or code}")
            return 1
        print(f"collector: started {UNIT}")
        return 0
    if dry_run:
        print(f"would write {path}, daemon-reload and enable --now {UNIT}")
        return 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(collector.render_unit(), encoding="utf-8")
    except OSError as exc:
        print(f"collector: cannot write {path}: {exc.strerror or exc}")
        return 1
    for step in (("daemon-reload",), ("enable", "--now", UNIT)):
        code, said = collector.systemctl(*step)
        if code != 0:
            print(f"collector: wrote {path}; `systemctl --user "
                  f"{' '.join(step)}` failed: {said or code}")
            return 1
    print(f"collector: wrote {path}, enabled and started {UNIT}")
    return 0


def _foreman_step(args: argparse.Namespace) -> int:
    """`launch foreman` with this verb's model and effort; one line."""
    if args.dry_run:
        print(f"would launch foreman --model {args.model} "
              f"--effort {args.effort}")
        return 0
    parser = argparse.ArgumentParser(prog="foreman launch")
    launch_module.add_launch_arguments(parser)
    argv = ["foreman", "--model", args.model, "--effort", args.effort]
    if args.repo:
        argv += ["--repo", args.repo]
    launch_args = parser.parse_args(argv)
    # The launch reports a screenful; this verb owes one line per step.
    # Its refusals go to stderr and pass through untouched.
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        code = launch_module.cmd_launch(launch_args)
    if code != 0:
        sys.stderr.write(said.getvalue())
        print("foreman: not started (the launch was refused)")
        return code
    live = launch_module.live_foreman()
    if not live:
        print("foreman: launched but not live on the roster")
        return 1
    session_id, _ = live[0]
    print(f"foreman: {session_id} ({args.model}, {args.effort})")
    return 0


def start_main(args: argparse.Namespace) -> int:
    me, violations = caller.resolve("start")
    # No role is allowed: the owner, from a terminal, is the only caller.
    caller.check_role(me, "start", violations=violations)
    live = launch_module.live_foreman()
    if live:
        violations.append(f"a foreman is live: {live[0][0]}; stop it first")
    if violations:
        return Refusal(violations).report()
    code = _collector_step(args.dry_run)
    if code != 0:
        return code
    return _foreman_step(args)


@subcommand("start",
            help="Bring up the collector unit and the foreman in one verb.")
def _start_entry(args: argparse.Namespace) -> int:
    return start_main(args)


_start_entry.add_arguments = add_start_arguments  # type: ignore[attr-defined]
