"""The ``muse`` pool adapter: headless Muse workers.

Launch shape (exactly the proven one): ``muse exec --model
muse-spark-1.3-contributor --reasoning-effort <high|xhigh>
--approval-mode never --json --workspace <worktree> --prompt-file
<FOREMAN-JOB.md>``, wrapped so the finish marker ``### finished rc=$?``
is written inside the redirected stream, not after the pipe::

    bash -c 'echo $$ > <pid file>; { muse exec ...; echo "### finished rc=$?"; } 2>&1 | tee <log>'

Approvals are off but the OS sandbox stays on: ``--yolo`` disables both,
and the sandbox is what the symlink write escape found in review needed.
Measured 2026-09-08: with ``--approval-mode never`` a worker still
reaches the network (curl 200) and a local database (sqlite read/write);
the logged-in ``gh`` (keyring auth) does NOT pass through the sandbox, so
a job needing pushes must say so rather than silently regress. The shell
writes its own pid to the session's ``pid`` file as its first act.

"Deny worker tools" for this pool is by omission, and that is observable
in the command: no MCP server is ever started for a worker, and no
tool-granting flag (agent overlay, plugin, MCP config) is passed — the
vendored CLI exposes no such flag to withhold. The role prompt states the
matching prohibition in plain words: a worker has no Foreman tools, calls
no Foreman verb, and writes to no ledger.

Detachment is configuration, not code, and headless is the default: a
worker runs through ``systemd-run --user --unit=foreman-<session>`` and
takes no desktop workspace. A launch that asks to be watched
(``foreman launch --window``) goes through the window launcher named by
the ``FOREMAN_WINDOW_LAUNCHER`` environment variable (wins) or the
``window_launcher`` key in the config file's ``[launch]`` table.
Configuring a launcher does not by itself open windows.

Wrapping, pid file, finish marker and ``observe`` live in
:mod:`foreman.pools._common`, shared with the grok and claude adapters;
this module keeps the vendor argv and the public names it always had.

``--json`` makes stdout a JSONL event stream; ``usage`` sums the
per-goal token attribution it carries (``goal_usage_attribution``
quantities, falling back to ``model_completed`` usages) — ``None`` when
the log carries neither, never an invented number.
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
    """The vendor command, exactly the proven shape. No tool flags: none pass.

    Approvals off (``--approval-mode never``), sandbox on: ``--yolo``
    would disable both.
    """
    return [
        "muse",
        "exec",
        "--model",
        MODEL,
        "--reasoning-effort",
        ctx.effort,
        "--approval-mode",
        "never",
        "--json",
        "--workspace",
        str(ctx.worktree),
        "--prompt-file",
        str(ctx.job_path),
    ]


def inner_command(ctx: LaunchContext) -> str:
    """Shell running the worker: pid first, marker inside the redirection."""
    return _common.wrap_inner(
        muse_argv(ctx),
        pid_path=ctx.pid_path,
        log_path=ctx.log_path,
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


def transcript_path(session: Session) -> Path:
    """Where this session's JSONL event stream lives: its session log."""
    raw = session.log or str(paths.session_log_path(session.id or ""))
    return Path(raw)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else None


def read_usage(transcript: Path) -> dict | None:
    """Input/output tokens from a ``--json`` event log, or nothing.

    Sums ``input_tokens``/``output_tokens`` over the run's per-goal
    attribution records (``goal_usage_attribution`` quantities), falling
    back to summing ``model_completed`` usages when the log carries no
    attribution. A malformed line is skipped, never fatal. Returns
    ``None`` when the log is missing or carries no usable counter: no
    number is invented.
    """
    try:
        text = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    attributed_in = attributed_out = 0
    found_attribution = False
    model_in = model_out = 0
    found_model = False
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
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        event = payload.get("event")
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        if kind == "goal_usage_attribution":
            record = event.get("record")
            quantity = record.get("quantity") if isinstance(
                record, dict) else None
            if not isinstance(quantity, dict):
                continue
            if quantity.get("reported") is False:
                continue
            inputs, outputs = _int(quantity.get("input_tokens")), _int(
                quantity.get("output_tokens"))
            if inputs is not None and outputs is not None:
                attributed_in += inputs
                attributed_out += outputs
                found_attribution = True
        elif kind == "model_completed":
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            inputs, outputs = _int(usage.get("input_tokens")), _int(
                usage.get("output_tokens"))
            if inputs is not None and outputs is not None:
                model_in += inputs
                model_out += outputs
                found_model = True
    if found_attribution:
        return {"input_tokens": attributed_in,
                "output_tokens": attributed_out}
    if found_model:
        return {"input_tokens": model_in, "output_tokens": model_out}
    return None


class MuseAdapter(PoolAdapter):
    name = "muse"
    model = MODEL
    binary = "muse"
    timeout_default = "20m"
    effort_default = "xhigh"
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
        """Input/output tokens from the ``--json`` log, else nothing."""
        return read_usage(transcript_path(session))

    def refusal(self, session: Session) -> dict | None:
        """Quota/rate refusal from the ``--json`` log, else nothing."""
        return _common.read_quota_refusal(transcript_path(session))
