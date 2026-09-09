"""The ``claude`` pool adapter: headless Claude workers.

Launch shape::

    claude -p --output-format stream-json --verbose \\
      --model <the role's model> --dangerously-skip-permissions \\
      --disallowedTools 'mcp__foreman__* mcp__boxes__*' \\
      --add-dir <worktree> < <FOREMAN-JOB.md>

``-p`` reads the spec from standard input (the job file is redirected in,
so the whole spec never travels as one argv element). The swarm's own
tools are denied through ``--disallowedTools``: on this CLI, MCP tools
surface as ``mcp__<server>__<tool>``, so both the Foreman server and the
board server are covered. ``--output-format stream-json`` makes stdout a
JSONL transcript; the wrapper tees it to the session log, so the log is
the recorded transcript path (see :func:`transcript_path`) and ``usage``
reads input/output tokens from its last ``usage`` object — ``None`` when
the transcript has none.

Wrapping, pid file, finish marker and ``observe`` are the shared ones in
:mod:`foreman.pools._common`, identical to the muse adapter.

Nothing here is left to the machine's own configuration. The working
directory is the worktree — the unit starts there
(``--working-directory``) and Claude is told too (``--add-dir``), since
``systemd-run --user`` otherwise starts in the caller's home — the model
is named for the role the job asked for, and the permission mode is on
the command line: a headless worker has nobody to answer an approval
prompt, and a worker whose model is whatever the machine happens to
default to is not the worker the supervisor asked for. Owner ruling,
2026-09-08: nothing is guessed at launch.
"""

from __future__ import annotations

import json
from pathlib import Path
from shlex import quote as _quote
from subprocess import Popen

from . import LaunchContext, PoolAdapter
from . import _common
from .. import paths
from ..entities import Session

#: Recorded on the session, and passed as ``--model``.
MODEL = "claude-opus-5"
#: The model each role that draws on this pool is run with. A role with no
#: entry runs the pool's own model; nothing runs on the machine default.
ROLE_MODELS = {"opus": "claude-opus-5", "supervisor": "claude-opus-5"}


def model_for(role: str) -> str:
    return ROLE_MODELS.get(role or "", MODEL)
#: Denied MCP tool patterns: the swarm's own servers, in this CLI's
#: ``mcp__<server>__<tool>`` spelling.
DISALLOWED_TOOLS = "mcp__foreman__* mcp__boxes__*"


def claude_argv(ctx: LaunchContext) -> list[str]:
    """The vendor command. The spec arrives on stdin (see inner_command).

    The model is the one the session was launched with. A user pool that
    wraps this adapter records its manifest model on the session, and
    that is what the vendor is started with. Only a session that carries
    none falls back to the role pin.
    """
    model = ctx.session.model or model_for(ctx.session.role)
    return [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--dangerously-skip-permissions",
        "--disallowedTools",
        DISALLOWED_TOOLS,
        "--add-dir",
        str(ctx.worktree),
    ]


def inner_command(ctx: LaunchContext) -> str:
    """Shell running the worker: pid first, marker inside the redirection.

    The job file is redirected into the vendor command's standard input;
    the JSONL transcript streams through the pipe into the session log.
    The worktree reaches the worker through the unit
    (``--working-directory``) and ``--add-dir`` above — no ``cd`` here.
    """
    return _common.wrap_inner(
        claude_argv(ctx),
        pid_path=ctx.pid_path,
        log_path=ctx.log_path,
        stdin_path=ctx.job_path,
    )


def outer_argv(ctx: LaunchContext) -> list[str]:
    """Detached spawn: headless, unless this launch asked for a window."""
    return _common.wrap_outer(ctx.session.id, inner_command(ctx),
                              window=ctx.window,
                              script_path=ctx.pid_path.parent / "run.sh",
                              worktree=ctx.worktree,
                              timeout=ctx.timeout)


def transcript_path(session: Session) -> Path:
    """Where this session's JSONL transcript lives: its session log."""
    raw = session.log or str(paths.session_log_path(session.id or ""))
    return Path(raw)


def read_usage(transcript: Path) -> dict | None:
    """Input/output tokens from a stream-json transcript, or nothing.

    Scans each JSON object per line (a malformed line is skipped, never
    fatal) for a ``usage`` mapping and takes the last one carrying both
    ``input_tokens`` and ``output_tokens`` as ints — the run's totals.
    Returns ``None`` when the transcript is missing or carries no usable
    usage object: no number is invented.
    """
    try:
        text = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            continue
        inputs = usage.get("input_tokens")
        outputs = usage.get("output_tokens")
        if isinstance(inputs, int) and isinstance(outputs, int):
            found = {"input_tokens": inputs, "output_tokens": outputs}
    return found


# Re-exported so existing readers keep one home for the shared names.
FINISH_RE = _common.FINISH_RE
WINDOW_LAUNCHER_ENV = _common.WINDOW_LAUNCHER_ENV
PID_WAIT_SECONDS = _common.PID_WAIT_SECONDS


def window_launcher() -> str | None:
    return _common.window_launcher()


def read_pid_file(path: Path) -> int | None:
    return _common.read_pid_file(path)


class ClaudeAdapter(PoolAdapter):
    name = "claude"
    model = MODEL
    binary = "claude"
    # The owner's own sessions run the same binary.
    binary_is_foreman_worker = False
    timeout_default = "20m"
    interactive = False

    def model_for_role(self, role: str) -> str:
        """The model this role runs on the packaged claude pool."""
        return model_for(role)

    def command_str(self, ctx: LaunchContext) -> str:
        return _common.printable_command(outer_argv(ctx), inner_command(ctx))

    def launch(self, ctx: LaunchContext) -> int:
        _common.write_worker_script(ctx.pid_path.parent / "run.sh",
                                    inner_command(ctx))
        return _common.spawn_and_wait(
            outer_argv(ctx), pid_path=ctx.pid_path,
            session_id=ctx.session.id, popen=Popen,
        )

    def observe(self, session: Session) -> dict:
        return _common.observe_session(session)

    def usage(self, session: Session) -> dict | None:
        """Input/output tokens from the transcript JSONL, else nothing."""
        return read_usage(transcript_path(session))

    def verdict(self, path: Path | str) -> dict:
        """A review job's verdict file, normalised to pass/fail + summary."""
        return _common.read_verdict(path)
