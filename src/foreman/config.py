"""The runtime configuration: what each pool may hold.

One file, read once per call, and the Omarchy doctrine end to end: the
packaged ``foreman.default.toml`` beside this module is the default and is
copied into the user's config directory the first time anything reads the
configuration and ``foreman.toml`` is not there; the user's file then
overrides it key by key, so a file naming one pool changes that pool and
leaves the rest at the shipped values; and a user file that does not parse
falls back to the packaged defaults with one warning on stderr naming the
file. Never a crash, and never a silent zero: a pool the configuration
does not name has no cap at all, not a cap of nothing.

``foreman cap <pool> <n>`` is the owner's verb over this file. It edits the
one line it owns and leaves every other byte alone, so thresholds another
part of the system reads from the same file survive it.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import caller, cli, paths
from .caller import Refusal

#: The packaged default, beside this module. Kept identical to
#: :data:`DEFAULT_TEXT`, which answers when the file is not on disk (an
#: installed package that dropped the data file).
DEFAULT_FILE = Path(__file__).with_name("foreman.default.toml")

DEFAULT_TEXT = """\
# Foreman's packaged defaults.
#
# This file is copied to ~/.config/foreman/foreman.toml the first time
# anything reads the configuration and that file does not exist. From then
# on the user's file is the one that is read, and it overrides these values
# key by key: a user file naming one pool changes that pool and leaves
# every other one at the value shipped here.
#
# `cap` is the most sessions the pool may hold at once, across every front.
# `supervisor_cap` is how many supervisors the pool may carry; a supervisor
# holds no job slot, so it is counted separately from `cap`.

[pool.muse]
cap = 5

[pool.opus]
cap = 2
supervisor_cap = 2

[pool.codex]
cap = 1

[pool.grok]
cap = 1
"""

#: The per-pool keys this module understands. Anything else in the table is
#: another reader's business and is carried through untouched.
POOL_KEYS = ("cap", "supervisor_cap")


@dataclass(frozen=True)
class Config:
    """The merged configuration and where it came from."""

    pools: dict[str, dict[str, int]] = field(default_factory=dict)
    path: Path | None = None
    #: False when the user's file was unreadable and the defaults answered.
    user_read: bool = True

    def cap(self, pool: str) -> int | None:
        """The pool's cap, or None when the configuration does not name it.

        None is "no cap", not zero: an unconfigured pool is unlimited, so a
        missing line can never quietly refuse every launch.
        """
        entry = self.pools.get(pool)
        value = entry.get("cap") if isinstance(entry, dict) else None
        return value if isinstance(value, int) else None

    def supervisor_cap(self, pool: str) -> int | None:
        """How many supervisors the pool may carry. Counted apart from
        ``cap`` because a supervisor holds no job slot."""
        entry = self.pools.get(pool)
        value = entry.get("supervisor_cap") if isinstance(entry, dict) else None
        return value if isinstance(value, int) else None

    def names(self) -> list[str]:
        return sorted(self.pools)


def packaged_text() -> str:
    try:
        return DEFAULT_FILE.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_TEXT


def _pools_from(raw: object) -> dict[str, dict[str, int]]:
    """The ``[pool.<name>]`` tables, keeping only the keys we understand."""
    pools: dict[str, dict[str, int]] = {}
    table = raw.get("pool") if isinstance(raw, dict) else None
    if not isinstance(table, dict):
        return pools
    for name, entry in table.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        kept: dict[str, int] = {}
        for key in POOL_KEYS:
            value = entry.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                kept[key] = value
        pools[name] = kept
    return pools


def defaults() -> dict[str, dict[str, int]]:
    try:
        return _pools_from(tomllib.loads(packaged_text()))
    except tomllib.TOMLDecodeError:  # pragma: no cover - the shipped file
        return _pools_from(tomllib.loads(DEFAULT_TEXT))


def ensure_user_config(create_dir: bool = False) -> Path:
    """Copy the packaged default into place the first time it is missing.

    The user owns the copy from that moment: Foreman never rewrites it
    behind their back, and `foreman cap` edits only the line it owns.

    Reading the configuration leaves the copy in an existing config
    directory and creates no directory to put one in. The launcher writes
    into the state directory, the worktree and the repository and nowhere
    else, and conjuring a config tree out of a launch would break that;
    ``foreman cap`` passes ``create_dir`` because writing the file is the
    whole of what it does.
    """
    path = paths.config_file()
    if path.exists():
        return path
    if not (create_dir or path.parent.is_dir()):
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(packaged_text(), encoding="utf-8")
    except OSError:
        # An unwritable config directory is not a reason to stop: the
        # packaged defaults still answer every question below.
        pass
    return path


def load() -> Config:
    """The configuration for this call: packaged defaults, then the user's
    file over them key by key. A file that does not parse warns once and
    the defaults answer."""
    merged = defaults()
    path = ensure_user_config()
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return Config(pools=merged, path=path, user_read=True)
    except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
        print(f"foreman: warning: {path} does not parse ({exc}); "
              f"using the packaged defaults", file=sys.stderr)
        return Config(pools=merged, path=path, user_read=False)
    for name, entry in _pools_from(raw).items():
        merged.setdefault(name, {}).update(entry)
    return Config(pools=merged, path=path, user_read=True)


# --------------------------------------------------------------------------
# `foreman cap <pool> <n>`: the owner's verb over the pool caps.
# --------------------------------------------------------------------------

_CAP_LINE = re.compile(r"\s*cap\s*=")


def set_cap(pool: str, count: int) -> Path:
    """Write ``cap`` for one pool into the user's file, byte-surgically.

    The file also carries other readers' settings (the collector's
    thresholds), so the whole file is never re-rendered: the pool's own
    ``cap`` line is replaced where it exists, added at the end of its table
    where the table exists without one, and a new table is appended
    otherwise.
    """
    path = ensure_user_config(create_dir=True)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    out: list[str] = []
    inside = False
    written = False
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            if inside and not written:
                out.append(f"cap = {count}")
                written = True
            inside = line.strip() == f"[pool.{pool}]"
        elif inside and _CAP_LINE.match(line):
            if not written:
                out.append(f"cap = {count}")
                written = True
            continue
        out.append(line)
    if inside and not written:
        out.append(f"cap = {count}")
        written = True
    if not written:
        if out and out[-1].strip():
            out.append("")
        out.append(f"[pool.{pool}]")
        out.append(f"cap = {count}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def known_pools() -> list[str]:
    """Every pool a cap may be set on: the registered adapters plus the
    pools the configuration already names. A pool in neither is a typo."""
    from .pools import names as adapter_names

    return sorted(set(adapter_names()) | set(load().pools))


def add_cap_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("pool", help="pool to cap")
    sub.add_argument("count", help="the most sessions it may hold at once")


@cli.subcommand("cap", help="Set how many sessions a pool may hold at once.")
def _cap_entry(args: argparse.Namespace) -> int:
    return cap_main(args.pool, args.count)


_cap_entry.add_arguments = add_cap_arguments  # type: ignore[attr-defined]


def cap_main(pool: str, count: str | int) -> int:
    me, violations = caller.resolve("cap")
    caller.check_role(me, "cap", violations=violations)
    name = (pool or "").strip()
    known = known_pools()
    if not name:
        violations.append("field 'pool' is required")
    elif name not in known:
        violations.append(f"unknown pool '{name}'; pools with a cap: "
                          + (", ".join(known) or "(none)"))
    number: int | None = None
    try:
        number = int(str(count).strip())
    except (TypeError, ValueError):
        violations.append(f"field 'count' must be a number (got '{count}')")
    if number is not None and number < 0:
        violations.append(
            f"field 'count' must not be negative (got {number})")
    if violations:
        return Refusal(violations).report()
    assert number is not None
    set_cap(name, number)
    print(f"{name}: cap {number}")
    return 0
