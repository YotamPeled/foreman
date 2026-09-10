"""Command-line entry point. Later jobs register their verbs in SUBCOMMANDS."""

from __future__ import annotations

import argparse
from collections.abc import Callable

from . import __version__

Handler = Callable[[argparse.Namespace], int]

SUBCOMMANDS: dict[str, tuple[Handler, dict]] = {}


def subcommand(name: str, **kwargs):
    def register(handler: Handler) -> Handler:
        SUBCOMMANDS[name] = (handler, kwargs)
        return handler

    return register


@subcommand("version", help="Print the package version.")
def cmd_version(args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foreman")
    verbs = parser.add_subparsers(dest="verb", required=True)
    for name, (handler, kwargs) in SUBCOMMANDS.items():
        sub = verbs.add_parser(name, **kwargs)
        add_arguments = getattr(handler, "add_arguments", None)
        if callable(add_arguments):
            add_arguments(sub)
        sub.set_defaults(_handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Importing here, not at module top, keeps the import cycle out: every verb
    # module imports this one for the decorator. Doing it before the parser is
    # built is what makes `python -m foreman.cli <verb>` find the verb at all.
    from . import attach as _attach  # noqa: F401
    from . import clockwork as _clockwork  # noqa: F401
    from . import collector as _collector  # noqa: F401
    from . import config as _config  # noqa: F401
    from . import doctor as _doctor  # noqa: F401
    from . import fronts as _fronts  # noqa: F401
    from . import headless as _headless  # noqa: F401
    from . import hooks as _hooks  # noqa: F401
    from . import landing as _landing  # noqa: F401
    from . import launch as _launch  # noqa: F401
    from . import mcp as _mcp  # noqa: F401
    from . import map as _map  # noqa: F401
    from . import milestone as _milestone  # noqa: F401
    from . import node as _node  # noqa: F401
    from . import panel_feed as _panel_feed  # noqa: F401
    from . import measure as _measure  # noqa: F401
    from . import merge as _merge  # noqa: F401
    from . import migrate as _migrate  # noqa: F401
    from . import pool as _pool  # noqa: F401
    from . import progress as _progress  # noqa: F401
    from . import resources as _resources  # noqa: F401
    from . import start as _start  # noqa: F401
    from . import status as _status  # noqa: F401
    from . import verbs as _verbs  # noqa: F401
    from . import wait as _wait  # noqa: F401
    from . import wake as _wake  # noqa: F401

    args = build_parser().parse_args(argv)
    from . import store as _store

    # Anything a caller wrote before the verb started is not this verb's
    # doing: the mark is cleared going in, so what is read on the way out
    # is exactly what this verb wrote.
    _store.take_written()
    code = args._handler(args)
    # The panel reads one summary file and no ledger, so a verb that moved
    # the state must leave that file moved too. Once, on the way out, and
    # only when something was actually written: a dry run and a refusal
    # write nothing, and that has to stay true of the summary as well.
    if args.verb != "panel-feed" and _store.take_written():
        _panel_feed.rewrite_quietly()
    return code
