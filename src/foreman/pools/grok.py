"""The ``grok`` pool adapter: headless Grok workers and reviewers.

Launch shape (exactly the proven one, file form of the prompt)::

    grok --prompt-file <FOREMAN-JOB.md> -m grok-4.6 --reasoning-effort <high|medium>
      --permission-mode bypassPermissions
      --deny 'foreman__*' --deny 'boxes__*'
      --disable-web-search --cwd <worktree>

Two owner rulings, both enforced here and locked by test. Every launch,
worker or reviewer, denies the swarm's own tools (``--deny 'foreman__*'``
and ``--deny 'boxes__*'``): the board server answers to any session id,
so a spec sentence alone does not hold. A review job (``kind == "review"``)
additionally passes ``--deny Write --deny Edit --deny Bash``: no sandbox
or permission-mode flag stops Grok writing — only those three denials do.

Wrapping, pid file, finish marker and ``observe`` are the shared ones in
:mod:`foreman.pools._common`, identical to the muse adapter.

Grok publishes no usage meter and no token counter, so ``usage`` returns
``None``; the collector shows jobs per hour for this pool instead. That
``None`` is the honest answer, not a missing feature — no number is
invented to fill it.
"""

from __future__ import annotations

from pathlib import Path
from shlex import quote as _quote
from subprocess import Popen

from . import LaunchContext, PoolAdapter
from . import _common
from ..entities import Session

MODEL = "grok-4.6"
#: Efforts the vendor flag accepts. The launcher offers the union over all
#: pools; ``ctx.effort`` is passed straight through.
EFFORTS = ("high", "medium")
#: Denials every launch carries: the swarm's own tools.
SWARM_DENIES = ("foreman__*", "boxes__*")
#: Extra denials a review job carries: the only thing proven to stop writes.
REVIEWER_DENIES = ("Write", "Edit", "Bash")


def grok_argv(ctx: LaunchContext, *, review: bool | None = None) -> list[str]:
    """The vendor command, exactly the proven shape.

    The reviewer variant (the three write denials) is selected by the
    job kind — ``review=None`` (the default) means ``ctx.kind`` decides —
    with an explicit ``review=True/False`` overriding it.
    """
    if review is None:
        review = ctx.kind == "review"
    argv = [
        "grok",
        "--prompt-file",
        str(ctx.job_path),
        "-m",
        MODEL,
        "--reasoning-effort",
        ctx.effort,
        "--permission-mode",
        "bypassPermissions",
    ]
    for pattern in SWARM_DENIES:
        argv += ["--deny", pattern]
    if review:
        for tool in REVIEWER_DENIES:
            argv += ["--deny", tool]
    argv += ["--disable-web-search", "--cwd", str(ctx.worktree)]
    return argv


def inner_command(ctx: LaunchContext) -> str:
    """Shell running the worker: pid first, marker inside the redirection."""
    return _common.wrap_inner(
        grok_argv(ctx),
        pid_path=ctx.pid_path,
        log_path=ctx.log_path,
    )


def outer_argv(ctx: LaunchContext) -> list[str]:
    """Detached spawn: headless, unless this launch asked for a window."""
    return _common.wrap_outer(ctx.session.id, inner_command(ctx),
                              window=ctx.window)


def read_pid_file(path: Path) -> int | None:
    return _common.read_pid_file(path)


# Re-exported so existing readers keep one home for the shared names.
FINISH_RE = _common.FINISH_RE
WINDOW_LAUNCHER_ENV = _common.WINDOW_LAUNCHER_ENV
PID_WAIT_SECONDS = _common.PID_WAIT_SECONDS


def window_launcher() -> str | None:
    return _common.window_launcher()


class GrokAdapter(PoolAdapter):
    name = "grok"
    model = MODEL
    timeout_default = "20m"
    interactive = False

    def command_str(self, ctx: LaunchContext) -> str:
        return " ".join(_quote(part) for part in outer_argv(ctx))

    def launch(self, ctx: LaunchContext) -> int:
        return _common.spawn_and_wait(
            outer_argv(ctx), pid_path=ctx.pid_path,
            session_id=ctx.session.id, popen=Popen,
        )

    def observe(self, session: Session) -> dict:
        return _common.observe_session(session)

    def usage(self, session: Session) -> dict | None:
        """Grok exposes no usage meter and no token counter: no number."""
        return None

    def verdict(self, path: Path | str) -> dict:
        """A review job's verdict file, normalised to pass/fail + summary."""
        return _common.read_verdict(path)
