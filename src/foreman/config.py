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

A pool's configuration key is the pool's *registered adapter name* — the
name :mod:`foreman.pools` answers to and the name a launch supplies — and
the worker roles that draw on it are declared beside it as ``roles``. That
is the one place the role-to-pool mapping lives: a table keyed on anything
else configures a pool nothing can launch, which is a cap the owner set
and the launcher never reads.

``foreman cap <pool> <n>`` is the owner's verb over this file. It parses
the document, edits the one value it owns and leaves every other line and
every comment byte-identical, and it validates the result before replacing
the file: a verb that lowers a cap must never be able to raise one by
appending a second table the loader then rejects.
"""

from __future__ import annotations

import argparse
import copy
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
# A table is named for the pool adapter it configures — `muse`, `claude`,
# `codex`, `grok` — because that is the name a launch supplies and the name
# the cap is checked against. `roles` declares the worker roles that draw on
# the pool, and is the only place that mapping is written down.
#
# `cap` is the most sessions the pool may hold at once, across every front.
# `supervisor_cap` is how many supervisors the pool may carry; a supervisor
# holds no job slot, so it is counted separately from `cap`.

# A windowed session — a supervisor, the merge desk, the foreman — opens on
# a workspace, and the shipped window helper takes it from the environment.
# A launch that names none used to wait thirty seconds for a window that
# could never appear and then blame the vendor, so a workspace is required
# configuration: `--workspace` names one per launch, and this is the one
# every launch that does not falls back to. The collector's automatic
# relaunch of a dead supervisor has no way to name one, so this is the
# value flow 4 runs on.
[launch]
default_workspace = 6

[pool.muse]
cap = 5
roles = ["muse"]

[pool.claude]
cap = 2
supervisor_cap = 2
roles = ["opus", "supervisor", "foreman"]

[pool.codex]
cap = 1
roles = ["astra"]

[pool.grok]
cap = 1
roles = ["grok"]
"""

#: The per-pool whole-number keys this module understands. Anything else in
#: the table is another reader's business and is carried through untouched.
POOL_KEYS = ("cap", "supervisor_cap")
#: The per-pool key naming the worker roles that run on the pool.
ROLES_KEY = "roles"


@dataclass(frozen=True)
class Config:
    """The merged configuration and where it came from."""

    pools: dict[str, dict[str, object]] = field(default_factory=dict)
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

    def roles(self, pool: str) -> tuple[str, ...]:
        """The worker roles declared as running on this pool."""
        entry = self.pools.get(pool)
        value = entry.get(ROLES_KEY) if isinstance(entry, dict) else None
        return tuple(value) if isinstance(value, tuple) else ()

    def pool_for_role(self, role: str) -> str | None:
        """The pool a worker role draws on, or None when nothing claims it.

        This is the whole role-to-pool mapping. It is read from the
        configuration rather than guessed from the role's name because the
        two differ for every pool whose adapter is named for its vendor:
        an ``opus`` worker runs on ``claude`` and an ``astra`` reviewer on
        ``codex``, and a cap keyed on the role governs nothing.
        """
        if not role:
            return None
        for name in sorted(self.pools):
            if role in self.roles(name):
                return name
        return None

    def names(self) -> list[str]:
        return sorted(self.pools)


def packaged_text() -> str:
    try:
        return DEFAULT_FILE.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_TEXT


def _pools_from(raw: object) -> dict[str, dict[str, object]]:
    """The ``[pool.<name>]`` tables, keeping only the keys we understand."""
    pools: dict[str, dict[str, object]] = {}
    table = raw.get("pool") if isinstance(raw, dict) else None
    if not isinstance(table, dict):
        return pools
    for name, entry in table.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        kept: dict[str, object] = {}
        for key in POOL_KEYS:
            value = entry.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                kept[key] = value
        roles = entry.get(ROLES_KEY)
        if isinstance(roles, list):
            kept[ROLES_KEY] = tuple(
                role for role in roles if isinstance(role, str) and role)
        pools[name] = kept
    return pools


def defaults() -> dict[str, dict[str, object]]:
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


#: Pools already warned about in this process, so a screen that reads the
#: configuration five times says it once. Keyed by file as well as pool: a
#: test that points the config elsewhere is a different file and warns again.
_WARNED: set[tuple[str, str]] = set()


def _registered_pools() -> frozenset[str]:
    """Pool names a launch can resolve, read at call time so a test that
    registers a fake pool is seen like any other."""
    from . import pools

    return frozenset(pools.names())


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
        # A cap on a pool no adapter registers governs nothing, silently.
        # That is exactly the defect this file was just fixed for: the
        # packaged default configured a pool called `opus` while the
        # adapter registers itself as `claude`, so the owner's cap on the
        # megalodon held nothing. A configuration left over from that
        # default is still on this machine, so say so rather than let a
        # number sit there looking like a limit.
        if name not in _registered_pools() and \
                (str(path), name) not in _WARNED:
            _WARNED.add((str(path), name))
            print(f"foreman: warning: {path} caps a pool '{name}' that no "
                  f"adapter registers, so that cap governs nothing",
                  file=sys.stderr)
        merged.setdefault(name, {}).update(entry)
    return Config(pools=merged, path=path, user_read=True)


# --------------------------------------------------------------------------
# `foreman cap <pool> <n>`: the owner's verb over the pool caps.
# --------------------------------------------------------------------------

#: ``cap`` as a key, bare or quoted, at the head of a line.
_CAP_LINE = re.compile(r"""\s*(cap|"cap"|'cap')\s*=""")


class CapRefused(Exception):
    """`foreman cap` will not touch the file: the reason, in one sentence."""


def _closing_bracket(text: str) -> int | None:
    """The index of the ``]`` that closes a table header, quotes respected.

    A header may carry a quoted key holding a bracket, and a comment after
    it may hold anything at all, so the closing bracket is found by
    scanning rather than by looking for the last character of the line.
    """
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "]":
            return index
        index += 1
    return None


def _unquote(part: str) -> str:
    part = part.strip()
    if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
        inner = part[1:-1]
        return inner.replace('\\"', '"') if part[0] == '"' else inner
    return part


def _dotted(inner: str) -> list[str]:
    """A header's dotted key, split on the dots that are not inside quotes."""
    parts: list[str] = []
    buffer = ""
    quote = ""
    index = 0
    while index < len(inner):
        char = inner[index]
        if quote:
            if char == "\\" and quote == '"':
                buffer += inner[index:index + 2]
                index += 2
                continue
            if char == quote:
                quote = ""
            buffer += char
        elif char in "\"'":
            quote = char
            buffer += char
        elif char == ".":
            parts.append(_unquote(buffer))
            buffer = ""
        else:
            buffer += char
        index += 1
    parts.append(_unquote(buffer))
    return parts


def _header_key(line: str) -> list[str] | None:
    """The table this line opens, or None when the line opens no table.

    ``[pool.muse]``, ``[ pool.muse ]``, ``[pool."muse"] # a comment`` are
    all the same table: matching the line as a literal string instead is
    what appended a second ``[pool.muse]`` to a file whose header carried
    a comment, and left the owner with a configuration that does not load.
    An array-of-tables header names no pool but still ends the table above
    it, so it answers with a key that matches nothing.
    """
    text = line.strip()
    if not text.startswith("["):
        return None
    if text.startswith("[["):
        return []
    end = _closing_bracket(text[1:])
    if end is None:
        return None
    rest = text[end + 2:].strip()
    if rest and not rest.startswith("#"):
        return None
    return _dotted(text[1:end + 1])


def _rewrite_cap(text: str, pool: str, count: int) -> str:
    """The file with this pool's ``cap`` set, every other line untouched."""
    out: list[str] = []
    inside = False
    written = False
    for line in text.splitlines():
        key = _header_key(line)
        if key is not None:
            if inside and not written:
                out.append(f"cap = {count}")
                written = True
            inside = key == ["pool", pool]
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
    body = "\n".join(out)
    return body + "\n" if not text or text.endswith("\n") else body


def set_cap(pool: str, count: int) -> Path:
    """Write ``cap`` for one pool into the user's file, byte-surgically.

    The file also carries other readers' settings (the collector's
    thresholds), so the whole file is never re-rendered: the pool's own
    ``cap`` line is replaced where it exists, added at the end of its table
    where the table exists without one, and a new table is appended
    otherwise.

    The edit is then read back and compared against the document the caller
    asked for, and the file is replaced only if the two agree. Anything the
    surgery got wrong is a refusal over an untouched file: this verb is how
    the owner stops a swarm, and a `cap` that leaves a configuration the
    loader rejects raises the effective limit instead of lowering it.
    """
    path = ensure_user_config(create_dir=True)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    try:
        current = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CapRefused(f"{path} does not parse ({exc}); fix the file "
                         f"before setting a cap on it") from None
    if not isinstance(current.get("pool", {}), dict):
        raise CapRefused(f"{path} uses 'pool' for something that is not a "
                         f"table of pools; fix the file by hand")
    want = copy.deepcopy(current)
    want.setdefault("pool", {}).setdefault(pool, {})["cap"] = count
    edited = _rewrite_cap(text, pool, count)
    try:
        got = tomllib.loads(edited)
    except tomllib.TOMLDecodeError:
        got = None
    if got != want:
        raise CapRefused(f"cannot set the cap in {path} without changing "
                         f"something else in it; edit the file by hand")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(edited, encoding="utf-8")
    return path


def known_pools() -> list[str]:
    """Every pool a cap may be set on: the registered adapters plus the
    pools the configuration already names. A pool in neither is a typo."""
    from .pools import names as adapter_names

    return sorted(set(adapter_names()) | set(load().pools))


def resolve_cap_name(name: str) -> str | None:
    """The pool ``foreman cap <name>`` means, or None when nothing does.

    A pool answers to its own name, and to the name of any worker role
    declared as running on it: the owner who caps ``opus`` is capping the
    Opus workers, and refusing that as a typo — or writing a `[pool.opus]`
    nothing checks — is the same unenforced limit either way.
    """
    if name in known_pools():
        return name
    return load().pool_for_role(name)


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
    target: str | None = None
    if not name:
        violations.append("field 'pool' is required")
    else:
        target = resolve_cap_name(name)
        if target is None:
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
    assert number is not None and target is not None
    try:
        set_cap(target, number)
    except CapRefused as exc:
        return Refusal([str(exc)]).report()
    if target != name:
        print(f"{target}: cap {number} (the pool role '{name}' runs on)")
    else:
        print(f"{target}: cap {number}")
    _reload_collector()
    return 0


def _reload_collector() -> None:
    """Push a collector reload now the ceiling it observes has changed.

    Best effort: a cap change must never fail on its reload.
    """
    from . import collector as _collector

    try:
        _collector.reload_after_config_change()
    except Exception:  # noqa: BLE001 - the reload is never the verb's work
        pass
