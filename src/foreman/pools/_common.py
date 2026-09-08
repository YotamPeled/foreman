"""Parts every headless pool adapter shares: the wrapper, the wait, observe.

Each vendor command runs the same way: a shell started as a process group
leader writes its own pid first, then runs the vendor CLI with the finish
marker ``### finished rc=$?`` inside the redirected stream (a marker echoed
after the pipe never reaches the log), tee'd to the session log::

    setsid --wait bash -c 'echo $$ > <pid file>; { <vendor ...>; echo "### finished rc=$?"; } 2>&1 | tee <log>'

``observe`` recognises completion only as a line of its own reading
``### finished rc=<n>``: a spec quoting the marker, or prose mentioning it,
is not completion. ``read_verdict`` is the one verdict-file reading every
review-capable adapter uses, so two pools never disagree about a review.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
import tomllib
from pathlib import Path
from subprocess import DEVNULL
from typing import Any

from .. import paths

#: Completion is a line of its own: the marker, ``rc=`` and the worker's
#: exit code. A mention anywhere else — a spec describing the marker, a
#: quoted line, a bare marker with no code — is not completion.
FINISH_RE = re.compile(r"^### finished rc=(\d+)$", re.MULTILINE)
WINDOW_LAUNCHER_ENV = "FOREMAN_WINDOW_LAUNCHER"
PID_WAIT_SECONDS = 30.0
#: Values the launcher accepts for ``--effort``: the union of what every
#: pool understands. Each adapter documents its own subset and passes
#: ``ctx.effort`` straight to its vendor flag.
LAUNCH_EFFORTS = ("high", "medium", "xhigh")


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


def default_window_launcher() -> str:
    """The shipped window helper, when nothing configures another one.

    A supervisor is an interactive session in a terminal window, so unlike
    a headless worker it has no systemd-run fallback: without a launcher
    there is no window and no session. The default is the helper shipped
    with the skills; the source names no home directory, so the path is
    composed here and overridden by ``FOREMAN_WINDOW_LAUNCHER`` or the
    config file like every other launcher choice.
    """
    return str(Path.home() / ".claude" / "skills" / "muse-workers"
               / "launch-window.sh")


def window_launcher_or_default() -> str:
    return window_launcher() or default_window_launcher()


def read_pid_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def wrap_inner(vendor_argv: list[str], *, pid_path: Path | str,
               log_path: Path | str, stdin_path: Path | str | None = None) -> str:
    """Shell running the worker: pid first, marker inside the redirection.

    ``setsid`` makes the shell that writes the pid file a process group
    leader, so the recorded pid is the group to signal later. ``stdin_path``
    redirects the vendor command's standard input from a file (for CLIs
    whose prompt arrives on stdin); the marker stays inside the braces so
    it always reaches the log.
    """
    vendor = " ".join(shlex.quote(part) for part in vendor_argv)
    if stdin_path is not None:
        vendor += " < " + shlex.quote(str(stdin_path))
    worker = (
        f"echo $$ > {shlex.quote(str(pid_path))}; "
        "{ " + vendor + '; echo "### finished rc=$?"; '
        "} 2>&1 | tee " + shlex.quote(str(log_path))
    )
    # setsid --wait, never plain setsid: the headless spawn is a
    # transient systemd unit with --collect, and plain setsid forks and
    # lets its parent exit at once, so systemd sees the unit's main
    # process finish and tears the cgroup down with the worker inside it.
    # Proven 2026-09-08: the pid file was never written and the job never
    # ran; with --wait the same command runs to completion.
    return "setsid --wait bash -c " + shlex.quote(worker)


def wrap_outer(session_id: str | None, inner: str, *,
               window: bool = False,
               script_path: Path | str | None = None) -> list[str]:
    """Detached spawn: headless, unless this launch asked for a window.

    Owner ruling: swarm sessions do not take the owner's desktop
    workspaces. A window is for watching one run and is asked for per
    launch; a configured window launcher on its own never opens one.

    The worker's shell is handed over as a script file, never as text on
    the command line, because ``systemd-run`` builds a systemd command
    line, where ``$$`` is the escape for a literal dollar and ``$WORD``
    is a unit variable. Proven 2026-09-08: passed as text, the worker
    wrote the two characters ``$`` and a newline into its pid file
    instead of its pid, and the launcher declared a launch failed while
    the job it had started ran to completion unobserved. A file has no
    such syntax.
    """
    run = ["bash", str(script_path)] if script_path else ["bash", "-c", inner]
    launcher = window_launcher() if window else None
    if launcher:
        return [launcher, f"foreman-{session_id}", *run]
    return [
        "systemd-run",
        "--user",
        "--collect",
        f"--unit=foreman-{session_id}",
        *run,
    ]


def printable_command(argv: list[str], inner: str) -> str:
    """What a dry run shows: how it is spawned, and what the worker runs.

    The spawn hands the worker over as a script file, so the argv alone
    would hide the vendor command; both lines are printed.
    """
    return (" ".join(shlex.quote(part) for part in argv)
            + "\n  worker script: " + inner)


def write_worker_script(path: Path | str, inner: str) -> Path:
    """Put the worker's shell on disk, where no other syntax touches it.

    Called only when a launch really starts; a dry run prints the same
    command and writes nothing.
    """
    script = Path(path)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/bash\n" + inner + "\n", encoding="utf-8")
    script.chmod(0o700)
    return script


def spawn_and_wait(argv: list[str], *, pid_path: Path,
                   session_id: str | None, popen: Any) -> int:
    """Start the wrapped command detached, return the worker's own pid.

    ``popen`` is the ``subprocess.Popen`` to spawn with, taken as a
    parameter so tests can substitute a double without touching the
    vendor CLI. The returned pid is read back from the pid file the
    wrapper writes as its first act — never the spawner's pid — and is
    the process group id by construction (``setsid``).
    """
    popen(argv, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
          start_new_session=True, close_fds=True)
    deadline = time.monotonic() + PID_WAIT_SECONDS
    while time.monotonic() < deadline:
        pid = read_pid_file(pid_path)
        if pid is not None:
            return pid
        time.sleep(0.1)
    raise RuntimeError(
        f"worker for session {session_id} wrote no pid file "
        f"at {pid_path} within {PID_WAIT_SECONDS:g}s; launch failed"
    )


def cpu_seconds(pid: int | None) -> float:
    """CPU seconds over the whole process tree below the recorded pid.

    The recorded pid is the wrapper shell started by the launcher, which
    does nothing but wait on its pipeline; its own utime and stime stay
    near zero while the vendor child burns seconds. Reading that pid
    alone makes every healthy job look idle and fires the stall
    detector on live work, so the tree below it is summed instead. The
    pid still identifies the job's process group for a later kill, which
    is what it is recorded for.
    """
    from .. import procs

    return procs.tree_cpu_seconds(pid)


def observe_session(session: Any) -> dict:
    """The collector's reading of one session: log mtime, CPU, finish."""
    # A launch with --log elsewhere writes completion there, so the
    # session carries its log path; the default covers older records.
    raw_log = session.log or str(paths.session_log_path(session.id or ""))
    log = Path(raw_log)
    try:
        transcript_mtime: float | None = log.stat().st_mtime
    except OSError:
        transcript_mtime = None
    finish_present = False
    finish_rc: int | None = None
    if transcript_mtime is not None:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        codes = FINISH_RE.findall(text)
        if codes:
            finish_present = True
            finish_rc = int(codes[-1])
    return {
        "transcript_mtime": transcript_mtime,
        "cpu_s": cpu_seconds(session.pid),
        "finish_present": finish_present,
        "finish_rc": finish_rc,
    }


_VERDICT_KEYS = ("passed", "pass", "verdict", "result", "status", "ok")
_VERDICT_TRUE = {"pass", "passed", "ok", "success", "successful", "true", "yes"}
_VERDICT_FALSE = {"fail", "failed", "failure", "error", "false", "no"}
_SUMMARY_KEYS = ("summary", "detail", "message", "reason")


def read_verdict(path: Path | str) -> dict:
    """Read a review job's verdict file, normalised to pass/fail + summary.

    Accepts the verdict under any of the usual keys (``passed``, ``pass``,
    ``verdict``, ``result``, ``status``, ``ok``) as a bool or a word like
    ``"pass"``/``"failed"``. Returns ``{"passed": bool, "summary": str}``.
    Raises ``ValueError`` naming the path when the file is missing, is not
    a JSON object, or names no verdict — a silent default here would send
    a task back (or land it) on no evidence.
    """
    where = Path(path)
    try:
        text = where.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"verdict file {where} unreadable: {exc.strerror or exc}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"verdict file {where} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"verdict file {where} is not a JSON object")
    passed: bool | None = None
    for key in _VERDICT_KEYS:
        value = data.get(key)
        if isinstance(value, bool):
            passed = value
            break
        if isinstance(value, str):
            word = value.strip().lower()
            if word in _VERDICT_TRUE:
                passed = True
                break
            if word in _VERDICT_FALSE:
                passed = False
                break
    if passed is None:
        raise ValueError(f"verdict file {where} names no pass/fail verdict")
    summary = ""
    for key in _SUMMARY_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            summary = value
            break
    return {"passed": passed, "summary": summary}
