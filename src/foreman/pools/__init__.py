"""Pool adapters: how a session becomes a process.

A pool is one model family (``muse`` today; ``grok`` and ``claude`` later).
Each pool is a plain module in this package exposing one adapter object
registered under the pool name. The launcher (``foreman.launch``) resolves
the pool, builds a :class:`LaunchContext` with every path already absolute,
and calls ``launch``; the collector will call ``observe``.

Adapter contract (the whole interface a new pool must implement):

- ``name``: pool name, the registry key.
- ``model``: model id recorded on the session and passed to the vendor CLI.
- ``timeout_default``: e.g. ``"20m"``; used unless ``--timeout`` overrides.
- ``interactive``: only ``interactive = True`` pools may open a window.
- ``launch(ctx) -> int``: start the worker detached, return its pid. The
  worker shell writes its own pid to ``ctx.pid_path`` as its first act;
  adapters should read that file back so the returned pid is the worker's,
  not a launcher's.
- ``observe(session) -> dict``: ``{"transcript_mtime": float | None,
  "cpu_s": float, "finish_present": bool}``.
- ``command_str(ctx) -> str``: the printable command line ``--dry-run``
  shows.

Later adapters (grok, claude) subclass :class:`PoolAdapter`, set the four
attributes, and implement the three methods; nothing else needs to change.
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


class PoolAdapter:
    """Base class for pool adapters. grok and claude fill this in later."""

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


REGISTRY: dict[str, PoolAdapter] = {}


def register(name: str, adapter: PoolAdapter) -> None:
    REGISTRY[name] = adapter


def unregister(name: str) -> None:
    REGISTRY.pop(name, None)


def get(name: str) -> PoolAdapter:
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "(none)"
        raise ValueError(f"unknown pool {name!r}; known pools: {known}") from None


def names() -> list[str]:
    return sorted(REGISTRY)


from .muse import MuseAdapter  # noqa: E402

register(MuseAdapter.name, MuseAdapter())
