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
- ``binary``: basename of the vendor executable (``grok``, ``muse``,
  ``claude``, ``codex``). Capacity counts processes whose argv[0]
  basename equals this. Default ``""``: a pool that names none counts
  nothing, never everything.
- ``binary_is_foreman_worker``: True when this pool's vendor binary
  only ever runs as a Foreman worker. False when the same binary is
  also the owner's own tool. Default True: a new pool is treated as
  Foreman-owned unless it says otherwise.
- ``timeout_default``: e.g. ``"20m"``; used unless ``--timeout`` overrides.
- ``effort_default``: one of ``low``, ``medium``, ``high``, ``xhigh``;
  used unless ``--effort`` overrides. A directory pool that omitted the
  key has ``None`` and a launch without ``--effort`` is refused naming
  the key to add. Default ``"high"`` so an in-memory adapter (a test
  fake) keeps today's behaviour.
- ``effort_min``: optional floor; a launch below it is refused. Muse
  ships ``xhigh`` (ruling rul-frzifag). Default ``None``.
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
- ``refusal(session) -> dict | None``: ``{"kind": "quota",
  "reset": "<iso8601>", "detail": "<one line>"}`` when this session's
  transcript shows the vendor refusing for capacity reasons and names
  a reset instant, else ``None`` — a pool is never put out on a guess.
- ``verdict(path) -> dict`` (review-capable pools): the review job's
  verdict file normalised to ``{"passed": bool, "summary": str}``; every
  pool uses the one reading in :mod:`foreman.pools._common`.

Wrapping, pid wait, observe and the verdict reading live in
:mod:`foreman.pools._common`; a new pool sets the four attributes and
delegates to it, adding only its vendor argv.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    #: How many units the job was launched to do. A count, never unit ids.
    units: int = 0
    kind: str = "implement"
    #: Open this worker in a terminal window, for watching a run. Off by
    #: owner ruling: swarm sessions do not take the owner's workspaces.
    window: bool = False


class PoolAdapter:
    """Base class for pool adapters."""

    name: str = ""
    model: str = ""
    binary: str = ""
    binary_is_foreman_worker: bool = True
    timeout_default: str = "20m"
    effort_default: str | None = "high"
    effort_min: str | None = None
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

    def refusal(self, session: Session) -> dict | None:
        """{"kind": "quota", "reset": "<iso8601>", "detail": "<one line>"}
        when this session's transcript shows the vendor refusing for
        capacity reasons, else None."""
        return None


REGISTRY: dict[str, PoolAdapter] = {}


def register(name: str, adapter: PoolAdapter) -> None:
    REGISTRY[name] = adapter


def unregister(name: str) -> None:
    REGISTRY.pop(name, None)


def _identity(name: str) -> tuple[str, tuple[str, ...]]:
    """The model and roles a registered pool answers to, from its manifest.

    A user directory wins; a packaged directory answers when there is no
    user copy. An adapter registered only in memory (a test fake) has no
    directory, so its ``model`` and optional ``roles`` attribute stand in.
    """
    from . import plugins as _plugins

    manifest, _source = _plugins.describe(name)
    if manifest is not None:
        return manifest.model, tuple(manifest.roles)
    adapter = REGISTRY.get(name)
    if adapter is None:
        return "", ()
    model = adapter.model if isinstance(adapter.model, str) else ""
    roles = getattr(adapter, "roles", ())
    if isinstance(roles, str):
        return model, (roles,)
    if isinstance(roles, (list, tuple)):
        return model, tuple(role for role in roles if isinstance(role, str))
    return model, ()


def suggest_pool(name: str) -> tuple[str, str] | None:
    """The registered pool whose model or roles contain ``name``.

    Case-insensitive substring. The first match in sorted pool-name
    order wins, so two pools sharing a fragment do not flip between
    runs. No match is ``None``: the unknown-pool message then stays
    what it was.
    """
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for pool in names():
        model, roles = _identity(pool)
        haystacks = (model, *roles)
        if any(needle in piece.lower() for piece in haystacks if piece):
            return pool, model
    return None


def unknown_pool_message(name: str, *, otherwise: str | None = None) -> str:
    """The one unknown-pool refusal, with a hint when a manifest matches.

    Launch, cap and the pool verbs all print this: the hint is made
    here so they cannot drift. ``otherwise`` is the suffix today's
    message used when nothing matches (``known pools: …`` from
    ``get``, ``pools with a cap: …`` from cap).
    """
    suggestion = suggest_pool(name)
    if suggestion is not None:
        pool, model = suggestion
        return f"unknown pool {name!r}; did you mean {pool} ({model})?"
    if otherwise is None:
        known = ", ".join(names()) or "(none)"
        otherwise = f"known pools: {known}"
    return f"unknown pool {name!r}; {otherwise}"


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
        raise ValueError(unknown_pool_message(name)) from None


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
