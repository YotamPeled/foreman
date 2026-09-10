"""Parts every headless pool adapter shares: the wrapper, the wait, observe.

Each vendor command runs the same way: a shell writes its own pid first,
then runs the vendor CLI with the finish marker ``### finished rc=$?``
inside the redirected stream (a marker echoed after the pipe never reaches
the log), tee'd to the session log::

    bash -c 'echo $$ > <pid file>; { <vendor ...>; echo "### finished rc=$?"; } 2>&1 | tee <log>'

The shell runs as its systemd unit's main process (``--service-type=exec``,
measured 2026-09-08: pid file written and marker delivered exactly as with
the old ``setsid --wait`` shape), so unit lifetime is job lifetime. The
working directory is the unit's (``--working-directory``), never a ``cd``
in the script. ``observe`` recognises completion only as a line of its own
reading ``### finished rc=<n>``: a spec quoting the marker, or prose
mentioning it, is not completion. ``read_verdict`` is the one verdict-file
reading every review-capable adapter uses, so two pools never disagree
about a review.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import tomllib
from datetime import datetime
from pathlib import Path
from subprocess import DEVNULL
from collections.abc import Collection
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
#: ``ctx.effort`` straight to its vendor flag. Order is rank: a pool's
#: ``effort_min`` refuses anything earlier in this tuple.
LAUNCH_EFFORTS = ("low", "medium", "high", "xhigh")

#: A vendor quota/rate refusal names when the window resets. Captured as
#: an ISO instant; anything we cannot parse is not a refusal.
_RESET_AT_RE = re.compile(
    r"resets\s+at\s+"
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))",
    re.IGNORECASE,
)
_QUOTA_RE = re.compile(
    r"(?:API error\s+)?429|quota exhausted|rate_limit|rate limit",
    re.IGNORECASE,
)

#: Record types that are never searched for a quota refusal, including
#: nested payloads. A worker quoting a proof clause is not the vendor.
NEVER_SEARCH_RECORD_TYPES = frozenset({
    "tool_call", "tool_result", "thought", "text", "user",
})


def parse_quota_refusal(text: str) -> dict | None:
    """{"kind": "quota", "reset": iso, "detail": one line}, or None.

    Both a capacity-refusal signal and a parseable reset instant are
    required: a rate-limit mention with no reset is not a guess we act
    on, and a reset with no refusal is not one either.
    """
    if not isinstance(text, str) or not text:
        return None
    if _QUOTA_RE.search(text) is None:
        return None
    match = _RESET_AT_RE.search(text)
    if match is None:
        return None
    reset = match.group(1)
    try:
        datetime.fromisoformat(reset.replace("Z", "+00:00"))
    except ValueError:
        return None
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end < 0:
        end = len(text)
    detail = " ".join(text[start:end].split())
    if not detail:
        return None
    return {"kind": "quota", "reset": reset, "detail": detail}


def record_type_of(obj: dict) -> str | None:
    """The vendor event type of one JSONL record, or None.

    Grok, Claude and Codex name it ``type`` (Codex also uses dotted
    kinds such as ``turn.failed``). Muse names it ``payload.event.kind``.
    ``record_type`` on a Muse envelope is the envelope class, not the
    event, and is ignored.
    """
    value = obj.get("type")
    if isinstance(value, str) and value:
        return value
    payload = obj.get("payload")
    if isinstance(payload, dict):
        event = payload.get("event")
        if isinstance(event, dict):
            kind = event.get("kind")
            if isinstance(kind, str) and kind:
                return kind
    kind = obj.get("kind")
    if isinstance(kind, str) and kind:
        return kind
    return None


def _json_object_line(line: str) -> dict | None:
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _record_text(obj: object) -> str:
    """String fields of one record, joined. Nested dicts and lists are
    walked only for a record already selected as an error kind."""
    parts: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            if value:
                parts.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return "\n".join(parts)


def _is_refusal_record(obj: dict, record_types: Collection[str]) -> bool:
    rtype = record_type_of(obj)
    if rtype is None or rtype in NEVER_SEARCH_RECORD_TYPES:
        return False
    if rtype not in record_types:
        return False
    if rtype == "result" and not obj.get("is_error"):
        return False
    return True


def read_quota_refusal(
    transcript: Path,
    record_types: Collection[str] | None = None,
) -> dict | None:
    """Quota refusal from a vendor JSON/JSONL log, or nothing.

    A JSONL transcript is walked record by record. A record is
    considered only when its type is one ``record_types`` names as an
    error or terminal-result kind; ``result`` additionally requires
    ``is_error``. ``tool_call``, ``tool_result``, ``thought``, ``text``
    and ``user`` records are never searched, nor their nested payloads.
    A non-JSON line is considered only when the file is not JSONL (a
    plain log). ``parse_quota_refusal`` keeps its contract for the text
    it is given.
    """
    try:
        text = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    kinds = frozenset(record_types or ())
    records: list[tuple[int, dict]] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or FINISH_RE.fullmatch(line):
            continue
        obj = _json_object_line(line)
        if obj is not None:
            records.append((line_no, obj))
    if records:
        for line_no, obj in records:
            if not _is_refusal_record(obj, kinds):
                continue
            found = parse_quota_refusal(_record_text(obj))
            if found is not None:
                found["record_type"] = record_type_of(obj)
                found["line_no"] = line_no
                return found
        return None
    found = parse_quota_refusal(text)
    if found is None:
        return None
    found["record_type"] = "log"
    found["line_no"] = 1
    reset = found.get("reset")
    if isinstance(reset, str) and reset:
        for line_no, raw in enumerate(text.splitlines(), 1):
            if reset in raw:
                found["line_no"] = line_no
                break
    return found


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


def timeout_seconds(timeout: str | None) -> int | None:
    """Seconds for a ``20m``-style job timeout, or None when unknown.

    Same grammar the launcher accepts (``s``/``m``/``h``/``d``, repeatable):
    ``RuntimeMaxSec`` needs a number, and an unparsed timeout must omit the
    property rather than invent one — Foreman's own kill still enforces it.
    """
    if not isinstance(timeout, str) or not timeout:
        return None
    total, number, seen = 0, "", False
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for char in timeout:
        if char.isdigit():
            number += char
        elif char in units and number:
            total += int(number) * units[char]
            number, seen = "", True
        else:
            return None
    if number or not seen:
        return None
    return total


def unit_name(session_id: str | None) -> str:
    """The transient systemd unit one session's worker runs as."""
    return f"foreman-{session_id}"


def wrap_inner(vendor_argv: list[str], *, pid_path: Path | str,
               log_path: Path | str,
               stdin_path: Path | str | None = None,
               env: dict[str, str] | None = None) -> str:
    """Shell running the worker: pid first, marker inside the redirection.

    The vendor process starts in the worktree because the *unit* does
    (``--working-directory``, set by :func:`wrap_outer`): no ``cd`` here,
    and nothing about a launch is left to be guessed. ``stdin_path``
    redirects the vendor command's standard input from a file (for CLIs
    whose prompt arrives on stdin); the marker stays inside the braces so
    it always reaches the log. ``env`` is exported inside the braces,
    before the vendor, so a systemd unit, a window and a dry run all
    carry the assignments — the pipeline would otherwise start the group
    in a subshell that never saw them.
    """
    vendor = " ".join(shlex.quote(part) for part in vendor_argv)
    if stdin_path is not None:
        vendor += " < " + shlex.quote(str(stdin_path))
    if env:
        assignments = " ".join(
            f"{name}={shlex.quote(value)}" for name, value in env.items())
        vendor = f"export {assignments}; {vendor}"
    worker = (
        f"echo $$ > {shlex.quote(str(pid_path))}; "
        "{ " + vendor + '; echo "### finished rc=$?"; '
        "} 2>&1 | tee " + shlex.quote(str(log_path))
    )
    # No setsid: the unit's main process is this shell itself
    # (``--service-type=exec``), so unit lifetime is job lifetime and the
    # cgroup is the kill group. Measured 2026-09-08 against the old
    # ``setsid --wait`` shape with a fake vendor plus a sleeping child:
    # both wrote the pid file and delivered the marker; ``systemctl stop``
    # reaps the whole tree either way.
    return "bash -c " + shlex.quote(worker)


def wrap_outer(session_id: str | None, inner: str, *,
               window: bool = False,
               script_path: Path | str | None = None,
               worktree: Path | str | None = None,
               timeout: str | None = None) -> list[str]:
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
    # No --collect: a collected unit is gone when it finishes, and the
    # same query then reads the defaults (Result=success, ExecMainStatus=0),
    # so a failed job reads as success. Measured 2026-09-08: without it a
    # unit that exited 7 still answers Result=exit-code ExecMainStatus=7.
    # The collector reads that status as the completion signal (the log
    # marker is the fallback) and runs `reset-failed` on its own schedule.
    argv = [
        "systemd-run",
        "--user",
        f"--unit={unit_name(session_id)}",
    ]
    if worktree is not None:
        # The worker starts in its worktree: no wrapper `cd` needed, and
        # no worker inherits the launcher's directory by accident.
        argv.append(f"--working-directory={worktree}")
    secs = timeout_seconds(timeout)
    if secs is not None:
        # systemd enforces the job timeout beside Foreman's own kill.
        argv.append(f"--property=RuntimeMaxSec={secs}")
    # The worker shell is the unit's main process, so unit lifetime is job
    # lifetime; `systemctl stop` is the kill. See wrap_inner for the probe.
    argv.append("--service-type=exec")
    return [*argv, *run]


#: Properties the collector reads off a worker's unit.
_UNIT_PROPERTIES = ("LoadState,ActiveState,Result,ExecMainStatus,"
                    "ExecMainCode")


def unit_status(session_id: str | None, *, run: Any = None) -> dict | None:
    """One worker unit's completion record, or None when unreadable.

    Runs ``systemctl --user show`` and returns the ``LoadState``,
    ``ActiveState``, ``Result`` and numeric ``ExecMainStatus`` (plus
    ``ExecMainCode``). A finished-but-uncollected unit keeps answering
    here; a never-started or already-cleaned one reads
    ``LoadState=not-found`` with success defaults, which is a real answer
    (fall back to the log marker), not a failure. ``None`` only when
    systemctl itself cannot be asked. ``run`` is the ``subprocess.run``
    to use, taken as a parameter so tests never shell out.
    """
    runner = run if run is not None else subprocess.run
    try:
        proc = runner(
            ["systemctl", "--user", "show", unit_name(session_id),
             f"--property={_UNIT_PROPERTIES}"],
            stdout=subprocess.PIPE, stderr=DEVNULL, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    status: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        key, equals, value = line.partition("=")
        if not equals or not key:
            continue
        if key in ("ExecMainStatus", "ExecMainCode"):
            try:
                status[key] = int(value.strip())
            except ValueError:
                status[key] = None
        else:
            status[key] = value
    return status


def unit_reports_failure(status: dict | None) -> bool:
    """True when a unit record says its job failed.

    A missing unit (``LoadState=not-found``) or an unreadable one (None)
    says nothing — the caller falls back to the log marker. Anything else
    whose ``ActiveState`` is failed with a non-success ``Result``, or whose
    ``Result`` is ``exit-code`` with a non-zero status, failed even if the
    log carries a finish marker.
    """
    if not status or status.get("LoadState") == "not-found":
        return False
    if status.get("ActiveState") == "failed" and \
            status.get("Result") not in (None, "success"):
        return True
    return status.get("Result") == "exit-code" and \
        (status.get("ExecMainStatus") or 0) != 0


def stop_unit(session_id: str | None, *, run: Any = None) -> bool:
    """Stop the transient unit one session's worker runs as.

    The unit is the kill group: its main process is the worker shell
    (``--service-type=exec``), so stopping the unit reaps the whole
    cgroup, children the launcher never saw included. This is what
    ``foreman kill`` aims at first, and the recorded pid is only the
    fallback for a session that has no unit — a supervisor in a window,
    or a worker whose unit is already gone.

    Best-effort and never raises: a unit that is not loaded, a machine
    with no user systemd, or a systemctl that cannot be run all read as
    False, which means "nothing was stopped here", never "the kill
    failed". Only the recorded session's own unit name is ever passed:
    nothing here matches a pattern.
    """
    runner = run if run is not None else subprocess.run
    try:
        proc = runner(
            ["systemctl", "--user", "stop", unit_name(session_id)],
            stdout=DEVNULL, stderr=DEVNULL, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def reset_failed_unit(session_id: str | None, *, run: Any = None) -> bool:
    """Forget one finished unit's result. Best-effort: never raises.

    The collector calls this on its own schedule after it has read a dead
    worker's status; without it every finished unit lingers as failed.
    Reading the status first is what makes this safe — resetting before
    the read would turn a failure into ``not-found`` success defaults.
    """
    runner = run if run is not None else subprocess.run
    try:
        proc = runner(
            ["systemctl", "--user", "reset-failed", unit_name(session_id)],
            stdout=DEVNULL, stderr=DEVNULL, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def printable_command(argv: list[str], inner: str) -> str:
    """What a dry run shows: how it is spawned, and what the worker runs.

    The spawn hands the worker over as a script file, so the argv alone
    would hide the vendor command; both lines are printed.
    """
    return (" ".join(shlex.quote(part) for part in argv)
            + "\n  worker script: " + inner)


def world_exports() -> str:
    """The state and config overrides the launcher is itself running under.

    A summoned session takes its session id from the environment and its
    state directory from the environment too, and only the first was ever
    carried. So a launcher running against an isolated ``FOREMAN_STATE``
    summoned a session into the *default* state directory, where the id it
    had just been given does not exist: every verb refused it as an
    unregistered writer, and the real ledger recorded the anomaly. The
    world travels with the session now, or the session is born in the
    wrong one. Empty when the launcher is on the default world, so a
    normal launch's script is unchanged.
    """
    lines = []
    for name in (paths.STATE_ENV, paths.CONFIG_ENV):
        value = os.environ.get(name)
        if value:
            lines.append(f"export {name}={shlex.quote(value)}\n")
    return "".join(lines)


def write_worker_script(path: Path | str, inner: str) -> Path:
    """Put the worker's shell on disk, where no other syntax touches it.

    Called only when a launch really starts; a dry run prints the same
    command and writes nothing. Every launch shape — worker, supervisor,
    merge desk, foreman — writes its script through here, so carrying the
    world here carries it for all four.
    """
    script = Path(path)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/bash\n" + world_exports() + inner + "\n",
                      encoding="utf-8")
    script.chmod(0o700)
    return script


def spawn_and_wait(argv: list[str], *, pid_path: Path,
                   session_id: str | None, popen: Any) -> int:
    """Start the wrapped command detached, return the worker's own pid.

    ``popen`` is the ``subprocess.Popen`` to spawn with, taken as a
    parameter so tests can substitute a double without touching the
    vendor CLI. The returned pid is read back from the pid file the
    wrapper writes as its first act — never the spawner's pid — and is
    the root of the worker's process tree, which is what a later kill
    walks.
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
