"""The ``grok`` pool adapter: headless Grok workers and reviewers.

Launch shape (exactly the proven one, file form of the prompt)::

    grok --prompt-file <FOREMAN-JOB.md> -m grok-4.6 --reasoning-effort <high|medium>
      --permission-mode bypassPermissions
      --deny 'MCPTool(foreman__*)' --deny 'MCPTool(boxes__*)'
      --disable-web-search --output-format streaming-json --cwd <worktree>

Two owner rulings, both enforced here and locked by test. Every launch,
worker or reviewer, denies the swarm's own tools
(``--deny 'MCPTool(foreman__*)'`` and ``--deny 'MCPTool(boxes__*)'``):
the board server answers to any session id, so a spec sentence alone
does not hold. The ``MCPTool(...)`` spelling is the documented
``ToolPrefix(glob)`` grammar — bare ``foreman__*`` names no prefix and
is silently accepted but unenforced (measured 2026-09-08: the old
spelling ran with no error, ``BogusPrefix(*)`` is rejected as unknown,
and the ``MCPTool(...)`` spelling is accepted). A review job
(``kind == "review"``) additionally passes ``--deny Write --deny Edit
--deny Bash``: no sandbox or permission-mode flag stops Grok writing —
only those three denials do. Those three are valid bare tool-name rules.

Wrapping, pid file, finish marker and ``observe`` are the shared ones in
:mod:`foreman.pools._common`, identical to the muse adapter. Every grok
inner command also exports :data:`CLAUDE_FAMILY_SWITCHES` as ``false``
before the vendor starts, so a unit, a window and a dry run all carry
them.

``--output-format streaming-json`` writes one JSON object per line
(NDJSON), so the first line lands in the log within seconds of start;
``json`` buffered until exit and a supervisor watching a long job saw
nothing. ``usage`` reads the final result/usage object among those
lines — and still reads the old single-object shape, so logs already on
disk answer. ``None`` where the log carries no usable counter, never an
invented number.
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

MODEL = "grok-4.6"
#: Efforts the vendor flag accepts. The launcher offers the union over all
#: pools; ``ctx.effort`` is passed straight through.
EFFORTS = ("high", "medium")
#: Denials every launch carries: the swarm's own tools, in the documented
#: ``ToolPrefix(glob)`` grammar (MCP tools are ``MCPTool(server__*)``).
SWARM_DENIES = ("MCPTool(foreman__*)", "MCPTool(boxes__*)")
#: Extra denials a review job carries: the only thing proven to stop writes.
REVIEWER_DENIES = ("Write", "Edit", "Bash")
#: Proven names from the corpus runner ``bin/gt_run.sh``. The grok CLI
#: reads the owner's Claude skills, agents, rules, MCP servers and hooks,
#: and Cursor skills, unless these six are ``false``.
CLAUDE_FAMILY_SWITCHES = (
    "GROK_CLAUDE_SKILLS_ENABLED",
    "GROK_CURSOR_SKILLS_ENABLED",
    "GROK_CLAUDE_AGENTS_ENABLED",
    "GROK_CLAUDE_RULES_ENABLED",
    "GROK_CLAUDE_MCPS_ENABLED",
    "GROK_CLAUDE_HOOKS_ENABLED",
)


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
    argv += ["--disable-web-search", "--output-format", "streaming-json",
             "--cwd", str(ctx.worktree)]
    return argv


def inner_command(ctx: LaunchContext) -> str:
    """Shell running the worker: pid first, marker inside the redirection.

    The six Claude-family switches are exported here, in the command
    itself, so a systemd unit, a window and a dry run all carry them.
    """
    return _common.wrap_inner(
        grok_argv(ctx),
        pid_path=ctx.pid_path,
        log_path=ctx.log_path,
        env={name: "false" for name in CLAUDE_FAMILY_SWITCHES},
    )


def outer_argv(ctx: LaunchContext) -> list[str]:
    """Detached spawn: headless, unless this launch asked for a window."""
    return _common.wrap_outer(ctx.session.id, inner_command(ctx),
                              window=ctx.window,
                              script_path=ctx.pid_path.parent / "run.sh",
                              worktree=ctx.worktree,
                              timeout=ctx.timeout)


def read_pid_file(path: Path) -> int | None:
    return _common.read_pid_file(path)


# Re-exported so existing readers keep one home for the shared names.
FINISH_RE = _common.FINISH_RE
WINDOW_LAUNCHER_ENV = _common.WINDOW_LAUNCHER_ENV
PID_WAIT_SECONDS = _common.PID_WAIT_SECONDS


def window_launcher() -> str | None:
    return _common.window_launcher()


def transcript_path(session: Session) -> Path:
    """Where this session's JSON result lives: its session log."""
    raw = session.log or str(paths.session_log_path(session.id or ""))
    return Path(raw)


def _tokens(usage: object) -> dict | None:
    """``{"input_tokens", "output_tokens"}`` from a usage mapping, or None."""
    if not isinstance(usage, dict):
        return None
    inputs = usage.get("input_tokens")
    outputs = usage.get("output_tokens")
    if isinstance(inputs, int) and not isinstance(inputs, bool) and \
            isinstance(outputs, int) and not isinstance(outputs, bool):
        return {"input_tokens": inputs, "output_tokens": outputs}
    return None


def read_usage(transcript: Path) -> dict | None:
    """Input/output tokens from a grok log, or nothing.

    ``--output-format streaming-json`` writes one JSON object per line;
    the last object carrying a usable ``usage`` mapping is the run's
    totals. Logs already on disk from ``--output-format json`` are a
    single result object, possibly spanning lines: the whole text minus
    marker lines is parsed first, then each single-line JSON object is
    tried, last one wins. Returns ``None`` when the log is missing or
    carries no usable ``usage`` object: no number is invented.
    """
    try:
        text = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    body = _common.FINISH_RE.sub("", text).strip()
    if body:
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            found = _tokens(obj.get("usage"))
            if found is not None:
                return found
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
        candidate = _tokens(obj.get("usage"))
        if candidate is not None:
            found = candidate
    return found


class GrokAdapter(PoolAdapter):
    name = "grok"
    model = MODEL
    binary = "grok"
    timeout_default = "20m"
    effort_default = "high"
    interactive = False

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
        """Input/output tokens from the session log."""
        return read_usage(transcript_path(session))

    def refusal(self, session: Session) -> dict | None:
        """Quota/rate refusal from the JSON log, else nothing."""
        return _common.read_quota_refusal(transcript_path(session))

    def verdict(self, path: Path | str) -> dict:
        """A review job's verdict file, normalised to pass/fail + summary."""
        return _common.read_verdict(path)
