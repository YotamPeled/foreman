"""The ``muse`` pool adapter: headless Muse workers.

Launch shape (exactly the proven one): ``muse exec --model
muse-spark-1.3-contributor --reasoning-effort <high|xhigh> --yolo
--workspace <worktree> --prompt-file <FOREMAN-JOB.md>``, wrapped so the
finish marker ``### finished rc=$?`` is written inside the redirected
stream, not after the pipe::

    setsid bash -c 'echo $$ > <pid file>; { muse exec ...; echo "### finished rc=$?"; } 2>&1 | tee <log>'

The wrapped command runs under ``setsid`` so the shell that writes the
pid file is a process group leader: the recorded pid is its pgid, and a
later kill of the group reaches the vendor process instead of orphaning
it. It writes its own pid to the session's ``pid`` file as its first act.

"Deny worker tools" for this pool is by omission, and that is observable
in the command: no MCP server is ever started for a worker, and no
tool-granting flag (agent overlay, plugin, MCP config) is passed — the
vendored CLI exposes no such flag to withhold. The role prompt states the
matching prohibition in plain words: a worker has no Foreman tools, calls
no Foreman verb, and writes to no ledger.

Detachment is configuration, not code, and headless is the default: a
worker runs through ``systemd-run --user --collect
--unit=foreman-<session>`` and takes no desktop workspace. A launch that
asks to be watched (``foreman launch --window``) goes through the window
launcher named by the ``FOREMAN_WINDOW_LAUNCHER`` environment variable
(wins) or the ``window_launcher`` key in the config file's ``[launch]``
table. Configuring a launcher does not by itself open windows.

Wrapping, pid file, finish marker and ``observe`` live in
:mod:`foreman.pools._common`, shared with the grok and claude adapters;
this module keeps the vendor argv and the public names it always had.

Muse publishes no token counter, so ``usage`` returns ``None``.
"""

from __future__ import annotations

from pathlib import Path
from shlex import quote as _quote
from subprocess import Popen

from . import LaunchContext, PoolAdapter
from . import _common
from ..entities import Session

MODEL = "muse-spark-1.3-contributor"
EFFORTS = ("high", "xhigh")

# Shared names, re-exported so this module keeps the surface it always had.
FINISH_RE = _common.FINISH_RE
WINDOW_LAUNCHER_ENV = _common.WINDOW_LAUNCHER_ENV
PID_WAIT_SECONDS = _common.PID_WAIT_SECONDS
_cpu_seconds = _common.cpu_seconds


def window_launcher() -> str | None:
    """Name of the configured window launcher, or None for systemd-run."""
    return _common.window_launcher()


def muse_argv(ctx: LaunchContext) -> list[str]:
    """The vendor command, exactly the proven shape. No tool flags: none pass."""
    return [
        "muse",
        "exec",
        "--model",
        MODEL,
        "--reasoning-effort",
        ctx.effort,
        "--yolo",
        "--workspace",
        str(ctx.worktree),
        "--prompt-file",
        str(ctx.job_path),
    ]


def inner_command(ctx: LaunchContext) -> str:
    """Shell running the worker: pid first, marker inside the redirection.

    ``setsid`` makes the shell that writes the pid file a process group
    leader, so the recorded pid is the group to signal later.
    """
    return _common.wrap_inner(
        muse_argv(ctx),
        pid_path=ctx.pid_path,
        log_path=ctx.log_path,
    )


def outer_argv(ctx: LaunchContext) -> list[str]:
    """Detached spawn: headless, unless this launch asked for a window."""
    return _common.wrap_outer(ctx.session.id, inner_command(ctx),
                              window=ctx.window)


def read_pid_file(path: Path) -> int | None:
    return _common.read_pid_file(path)


class MuseAdapter(PoolAdapter):
    name = "muse"
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
        """Muse publishes no token counter: no number."""
        return None
