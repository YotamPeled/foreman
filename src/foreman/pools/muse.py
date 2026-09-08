"""The ``muse`` pool adapter: headless Muse workers.

Launch shape (exactly the proven one): ``muse exec --model
muse-spark-1.3-contributor --reasoning-effort <high|xhigh> --yolo
--workspace <worktree> --prompt-file <FOREMAN-JOB.md>``, wrapped so the
finish marker ``### finished rc=$?`` is written inside the redirected
stream, not after the pipe::

    echo $$ > <pid file>; { muse exec ...; echo "### finished rc=$?"; } 2>&1 | tee <log>

The wrapped command writes its own pid to the session's ``pid`` file as
its first act.

"Deny worker tools" for this pool is by omission, and that is observable
in the command: no MCP server is ever started for a worker, and no
tool-granting flag (agent overlay, plugin, MCP config) is passed — the
vendored CLI exposes no such flag to withhold. The role prompt states the
matching prohibition in plain words: a worker has no Foreman tools, calls
no Foreman verb, and writes to no ledger.

Detachment is configuration, not code. When a window launcher is named —
by the ``FOREMAN_WINDOW_LAUNCHER`` environment variable (wins) or by the
``window_launcher`` key in the config file's ``[launch]`` table — the
adapter invokes it as ``<launcher> <name> bash -c <command>``. With none
configured it starts the command through ``systemd-run --user --collect
--unit=foreman-<session>``.
"""

from __future__ import annotations

import os
import shlex
import time
import tomllib
from pathlib import Path
from subprocess import DEVNULL, Popen

from . import LaunchContext, PoolAdapter
from .. import paths
from ..entities import Session

MODEL = "muse-spark-1.3-contributor"
EFFORTS = ("high", "xhigh")
FINISH_MARKER = "### finished"
WINDOW_LAUNCHER_ENV = "FOREMAN_WINDOW_LAUNCHER"
PID_WAIT_SECONDS = 30.0


def window_launcher() -> str | None:
    """Name of the configured window launcher, or None for systemd-run."""
    override = os.environ.get(WINDOW_LAUNCHER_ENV, "").strip()
    if override:
        return override
    try:
        with open(paths.config_file(), "rb") as handle:
            config = tomllib.load(handle)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError, ValueError):
        return None
    launch = config.get("launch")
    if isinstance(launch, dict) and launch.get("window_launcher"):
        return str(launch["window_launcher"])
    if config.get("window_launcher"):
        return str(config["window_launcher"])
    return None


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
    """Shell running the worker: pid first, marker inside the redirection."""
    vendor = " ".join(shlex.quote(part) for part in muse_argv(ctx))
    return (
        f"echo $$ > {shlex.quote(str(ctx.pid_path))}; "
        "{ " + vendor + '; echo "### finished rc=$?"; '
        "} 2>&1 | tee " + shlex.quote(str(ctx.log_path))
    )


def outer_argv(ctx: LaunchContext) -> list[str]:
    """Detached spawn: window launcher when configured, else systemd-run."""
    inner = inner_command(ctx)
    launcher = window_launcher()
    if launcher:
        return [launcher, f"foreman-{ctx.session.id}", "bash", "-c", inner]
    return [
        "systemd-run",
        "--user",
        "--collect",
        f"--unit=foreman-{ctx.session.id}",
        "bash",
        "-c",
        inner,
    ]


def read_pid_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


class MuseAdapter(PoolAdapter):
    name = "muse"
    model = MODEL
    timeout_default = "20m"
    interactive = False

    def command_str(self, ctx: LaunchContext) -> str:
        return " ".join(shlex.quote(part) for part in outer_argv(ctx))

    def launch(self, ctx: LaunchContext) -> int:
        argv = outer_argv(ctx)
        Popen(argv, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
              start_new_session=True, close_fds=True)
        deadline = time.monotonic() + PID_WAIT_SECONDS
        while time.monotonic() < deadline:
            pid = read_pid_file(ctx.pid_path)
            if pid is not None:
                return pid
            time.sleep(0.1)
        raise RuntimeError(
            f"worker for session {ctx.session.id} wrote no pid file "
            f"at {ctx.pid_path} within {PID_WAIT_SECONDS:g}s; launch failed"
        )

    def observe(self, session: Session) -> dict:
        log = paths.session_log_path(session.id or "")
        try:
            transcript_mtime: float | None = log.stat().st_mtime
        except OSError:
            transcript_mtime = None
        finish_present = False
        if transcript_mtime is not None:
            try:
                finish_present = FINISH_MARKER in log.read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                finish_present = False
        return {
            "transcript_mtime": transcript_mtime,
            "cpu_s": _cpu_seconds(session.pid),
            "finish_present": finish_present,
        }


def _cpu_seconds(pid: int | None) -> float:
    if pid is None:
        return 0.0
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            parts = handle.read().rsplit(")", 1)[1].split()
        ticks = float(parts[11]) + float(parts[12])
        return ticks / os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    except (OSError, ValueError, IndexError, KeyError):
        return 0.0
