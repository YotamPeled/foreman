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
        sub.set_defaults(_handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args._handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
