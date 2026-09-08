"""Pool adapters: how a session becomes a process.

A pool is one model family (``muse``, ``grok``, ``claude``, ``codex``)
and, since the plugin-directories job, a directory: each packaged pool
ships ``manifest.toml`` (name, model, timeout, interactive, roles, the
adapter implementing it) and ``SKILL.md`` beside its adapter module in
this package. The launcher (``foreman.launch``) resolves the pool,
builds a :class:`LaunchContext` with every path already absolute, and
calls ``launch``; the collector will call ``observe``.

A user pool directory (see :mod:`foreman.pools.plugins`) replaces the
packaged pool of the same name entirely; a broken one falls back to the
packaged pool with one warning, never a crash.

Adapter contract (the whole interface a new pool must implement):

- ``name``: pool name, the registry key.
- ``model``: model id recorded on the session and passed to the vendor CLI.
- ``timeout_default``: e.g. ``"20m"``; used unless ``--timeout`` overrides.
- ``interactive``: only ``interactive = True`` pools may open a window.
- ``launch(ctx) -> int``: start the worker detached, return its pid. The
  worker shell writes its own pid to ``ctx.pid_path`` as its first act;
  adapters should read that file back so the returned pid is the worker's,
  not a launcher's. The shell is started as a process group leader, so
  the returned pid is also the pgid the launcher records for a later kill.
- ``observe(session) -> dict``: ``{"transcript_mtime": float | None,
  "cpu_s": float, "finish_present": bool, "finish_rc": int | None}``.
  The log is read from ``session.log`` (the default path covers older
  records); ``finish_present`` is true only for a line of its own reading
  ``### finished rc=<n>``, whose number is ``finish_rc``.
- ``command_str(ctx) -> str``: the printable command line ``--dry-run``
  shows.
- ``usage(session) -> dict | None``: ``{"input_tokens": int,
  "output_tokens": int}`` where the vendor exposes a counter, else
  ``None`` — a pool with no counter reports nothing, never an invention.
- ``verdict(path) -> dict`` (review-capable pools): the review job's
  verdict file normalised to ``{"passed": bool, "summary": str}``; every
  pool uses the one reading in :mod:`foreman.pools._common`.

Wrapping, pid wait, observe and the verdict reading live in
:mod:`foreman.pools._common`; a new pool sets the four attributes and
delegates to it, adding only its vendor argv.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..entities import Session


@dataclass(frozen=True)
class LaunchContext:
    """Everything an adapter needs to start one worker. All paths absolute."""

    session: Session
    worktree: Path
    job_path: Path
    role_path: Path
    log_path: Path
    pid_path: Path
    verdict_path: Path
    scratch_dir: Path
    branch: str
    target: str
    timeout: str
    effort: str = "high"
    units: tuple[int, ...] = field(default_factory=tuple)
    kind: str = "implement"
    #: Open this worker in a terminal window, for watching a run. Off by
    #: owner ruling: swarm sessions do not take the owner's workspaces.
    window: bool = False


class PoolAdapter:
    """Base class for pool adapters."""

    name: str = ""
    model: str = ""
    timeout_default: str = "20m"
    interactive: bool = False

    def launch(self, ctx: LaunchContext) -> int:
        raise NotImplementedError

    def observe(self, session: Session) -> dict:
        raise NotImplementedError

    def command_str(self, ctx: LaunchContext) -> str:
        raise NotImplementedError

    def usage(self, session: Session) -> dict | None:
        """Token use, or None where the vendor exposes no counter."""
        return None


REGISTRY: dict[str, PoolAdapter] = {}


def register(name: str, adapter: PoolAdapter) -> None:
    REGISTRY[name] = adapter


def unregister(name: str) -> None:
    REGISTRY.pop(name, None)


def get(name: str) -> PoolAdapter:
    # A user pool directory replaces the packaged pool of the same name
    # entirely; a broken one warns once and falls through to the
    # packaged adapter below. The registry still answers the packaged
    # pools (and any adapter a test registers) directly, so a packaged
    # pool's behaviour stays byte for byte its adapter's.
    from . import plugins as _plugins

    override = _plugins.read_user_manifest(name, warn=True)
    if override is not None:
        return _plugins.DirectoryPool(override)
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(names()) or "(none)"
        raise ValueError(f"unknown pool {name!r}; known pools: {known}") from None


def names() -> list[str]:
    # The registered adapters plus the user's valid pool directories. A
    # user directory whose manifest does not validate is skipped quietly
    # here; resolving it warns (see :func:`get`).
    from . import plugins as _plugins

    return sorted(set(REGISTRY) | set(_plugins.valid_user_names()))


from .muse import MuseAdapter  # noqa: E402
from .grok import GrokAdapter  # noqa: E402
from .claude import ClaudeAdapter  # noqa: E402
from .codex import CodexAdapter  # noqa: E402

register(MuseAdapter.name, MuseAdapter())
register(GrokAdapter.name, GrokAdapter())
register(ClaudeAdapter.name, ClaudeAdapter())
register(CodexAdapter.name, CodexAdapter())
