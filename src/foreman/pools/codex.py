"""The ``codex`` pool adapter: headless GPT-6 Astra reviewers.

Launch shape (exactly the verified one, prompt on stdin)::

    codex exec --model gpt-6-astra -c model_reasoning_effort="<effort>" \\
      --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check \\
      -C <worktree> --output-schema <schema file> \\
      -o <session dir>/last-message.txt - < <FOREMAN-JOB.md>

The prompt goes in on standard input, with ``-`` as the positional
argument, so the whole job file never travels as one argv element.
``-o``/``--output-last-message`` writes the agent's last message, not the
file the reviewer writes: it points at ``last-message.txt`` beside the
verdict file, never at the verdict path, and the adapter reads the
verdict with the shared :func:`foreman.pools._common.read_verdict`.
``--output-schema`` is what makes that last message verdict JSON rather
than prose; the schema ships as a file in this package.

Reasoning effort is a config override, not a flag:
``-c model_reasoning_effort="<effort>"``. The launcher offers
``high``/``medium``/``xhigh``; Codex has no fourth level, so the mapping
lives in :func:`codex_effort`, which maps ``xhigh`` to ``high``.

The working directory must be trusted or the first screen is a question
nobody will answer: :func:`ensure_trust` adds the project table before
the launch. Codex writes a rollout transcript under its sessions
directory; :meth:`CodexAdapter.observe` records its path for a person to
read later, and the collector never depends on it. Codex exposes no
meter this adapter can read, so ``usage`` returns ``None``.

Wrapping, pid file, finish marker and ``observe`` are the shared ones in
:mod:`foreman.pools._common`, identical to the grok adapter.
"""

from __future__ import annotations

import os
from pathlib import Path
from subprocess import Popen

from . import LaunchContext, PoolAdapter
from . import _common
from .. import paths
from ..entities import Session

MODEL = "gpt-6-astra"
#: Efforts the launcher may pass. Codex has no fourth level: ``xhigh``
#: runs as ``high`` (see :func:`codex_effort`, the one place that maps).
EFFORTS = ("high", "medium")
SCHEMA_FILENAME = "codex_verdict.schema.json"
LAST_MESSAGE_FILENAME = "last-message.txt"
#: Codex's own home, honoured so a test never touches the real one.
CODEX_HOME_ENV = "CODEX_HOME"
#: The transcript pointer ``observe`` leaves in the session directory.
ROLLOUT_FILENAME = "vendor-rollout"


def codex_effort(effort: str) -> str:
    """Launcher effort mapped onto what Codex accepts.

    The launcher offers ``high``/``medium``/``xhigh``; Codex has no
    fourth level, so ``xhigh`` runs as ``high``. This function is where
    that mapping lives: nothing else translates effort for this pool.
    """
    return "high" if effort == "xhigh" else effort


def schema_path() -> Path:
    """The shipped verdict schema file passed as ``--output-schema``."""
    return Path(__file__).with_name(SCHEMA_FILENAME)


def last_message_path(ctx: LaunchContext) -> Path:
    """Where ``-o`` writes the agent's last message: beside the verdict.

    Never the verdict path itself: pointing both at one file lets the
    last message overwrite the reviewer's verdict JSON.
    """
    return ctx.verdict_path.parent / LAST_MESSAGE_FILENAME


def codex_argv(ctx: LaunchContext) -> list[str]:
    """The vendor command, exactly the verified shape.

    The prompt arrives on stdin (see :func:`inner_command`); the lone
    ``-`` positional tells ``codex exec`` to read it there.
    """
    return [
        "codex",
        "exec",
        "--model",
        MODEL,
        "-c",
        f'model_reasoning_effort="{codex_effort(ctx.effort)}"',
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C",
        str(ctx.worktree),
        "--output-schema",
        str(schema_path()),
        "-o",
        str(last_message_path(ctx)),
        "-",
    ]


def inner_command(ctx: LaunchContext) -> str:
    """Shell running the worker: pid first, marker inside the redirection.

    The job file is redirected into the vendor command's standard input.
    """
    return _common.wrap_inner(
        codex_argv(ctx),
        pid_path=ctx.pid_path,
        log_path=ctx.log_path,
        stdin_path=ctx.job_path,
        cwd=ctx.worktree,
    )


def outer_argv(ctx: LaunchContext) -> list[str]:
    """Detached spawn: headless, unless this launch asked for a window."""
    return _common.wrap_outer(ctx.session.id, inner_command(ctx),
                              window=ctx.window,
                              script_path=ctx.pid_path.parent / "run.sh")


def read_pid_file(path: Path) -> int | None:
    return _common.read_pid_file(path)


# Re-exported so existing readers keep one home for the shared names.
FINISH_RE = _common.FINISH_RE
WINDOW_LAUNCHER_ENV = _common.WINDOW_LAUNCHER_ENV
PID_WAIT_SECONDS = _common.PID_WAIT_SECONDS


def window_launcher() -> str | None:
    return _common.window_launcher()


def codex_home() -> Path:
    """Codex's home directory: ``$CODEX_HOME``, else ``~/.codex``.

    Read at call time so a test pointing ``HOME`` or ``CODEX_HOME`` at a
    temp dir redirects every path below, never the real config.
    """
    override = os.environ.get(CODEX_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def config_path() -> Path:
    """The Codex config file the trust edit reads and writes."""
    return codex_home() / "config.toml"


def _toml_string(value: str) -> str:
    """A TOML basic string: quoted, backslashes and quotes escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def project_header(worktree: Path | str) -> str:
    """The config table naming one trusted working directory."""
    return "[projects." + _toml_string(str(worktree)) + "]"


def ensure_trust(worktree: Path | str) -> Path:
    """Trust ``worktree`` in the Codex config, adding only that table.

    Reads the config file, appends ``[projects."<worktree>"]`` with
    ``trust_level = "trusted"`` when no such table is there, and writes
    it back atomically. Every other line stays byte-identical; a config
    already naming the table is left untouched, and one that cannot be
    read is left exactly as it is. A missing file is created holding
    just the new table. The config is never rewritten from a template.
    """
    path = config_path()
    header = project_header(worktree)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = None
    except OSError:
        return path
    if existing is None:
        updated = header + '\ntrust_level = "trusted"\n'
        mode: int | None = None
    else:
        for line in existing.splitlines():
            if line.strip() == header:
                return path
        updated = existing
        if updated:
            if not updated.endswith("\n"):
                updated += "\n"
            if not updated.endswith("\n\n"):
                updated += "\n"
        updated += header + '\ntrust_level = "trusted"\n'
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    return path


def sessions_root() -> Path:
    """Where Codex writes its rollout transcripts."""
    return codex_home() / "sessions"


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def find_rollout(session: Session) -> Path | None:
    """This session's Codex rollout transcript, when one can be found.

    The newest ``rollout-*.jsonl`` under the Codex sessions directory
    that is no older than the session log: a best-effort pointer for a
    person reading the transcript later, never evidence of liveness.
    ``None`` when the directory, the log, or any fresher rollout is
    missing — the caller leaves the pointer absent then.
    """
    try:
        candidates = [entry for entry in sessions_root().rglob(
            "rollout-*.jsonl") if entry.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    threshold: float | None = None
    if session.id and session.log:
        threshold = _mtime(Path(session.log))
    elif session.id:
        threshold = _mtime(paths.session_log_path(session.id))
    fresh = []
    for entry in candidates:
        stamp = _mtime(entry)
        if stamp is None:
            continue
        if threshold is not None and stamp < threshold:
            continue
        fresh.append((stamp, entry))
    if not fresh:
        return None
    return max(fresh)[1]


def record_rollout(session: Session) -> Path | None:
    """Write the rollout pointer into the session directory, once.

    Records the path :func:`find_rollout` finds as ``vendor-rollout``
    and leaves an existing pointer alone, so a later tick never
    re-points an old session at a newer run's transcript. Returns the
    recorded path, or ``None`` when nothing was recorded.
    """
    if not session.id:
        return None
    dest = paths.session_dir(session.id) / ROLLOUT_FILENAME
    if dest.exists():
        return Path(dest.read_text(encoding="utf-8").strip())
    found = find_rollout(session)
    if found is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(str(found) + "\n", encoding="utf-8")
    return found


class CodexAdapter(PoolAdapter):
    name = "codex"
    model = MODEL
    timeout_default = "25m"
    interactive = False

    def command_str(self, ctx: LaunchContext) -> str:
        return _common.printable_command(outer_argv(ctx), inner_command(ctx))

    def launch(self, ctx: LaunchContext) -> int:
        ensure_trust(ctx.worktree)
        _common.write_worker_script(ctx.pid_path.parent / "run.sh",
                                    inner_command(ctx))
        return _common.spawn_and_wait(
            outer_argv(ctx), pid_path=ctx.pid_path,
            session_id=ctx.session.id, popen=Popen,
        )

    def observe(self, session: Session) -> dict:
        try:
            record_rollout(session)
        except OSError:
            pass
        return _common.observe_session(session)

    def usage(self, session: Session) -> dict | None:
        """Codex exposes no meter this adapter can read: no number."""
        return None

    def verdict(self, path: Path | str) -> dict:
        """A review job's verdict file, normalised to pass/fail + summary."""
        return _common.read_verdict(path)
