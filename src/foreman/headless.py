"""Headless turns for supervisors and the merge desk.

A headless session is a Claude conversation that exists as a process only
while a turn is running. Between turns it is a roster row, a vendor
session id and a queue of wake events. One turn is one ``claude -p``
process under a transient systemd unit with ``RuntimeMaxSec`` set from
``[launch] turn_timeout`` (default 15 minutes), carrying the world the
way every other launch does so a turn in an isolated world stays in it.

- First turn: the role prompt as the prompt, the session's MCP config,
  ``--output-format stream-json``. The vendor session id is recorded off
  the stream. (``--model`` and ``--dangerously-skip-permissions`` travel
  too: a headless turn has nobody to answer an approval prompt, and a
  turn on the machine's default model is not the supervisor summoned.)
- Every later turn: ``claude -p --resume <vendor id> "<event text>"``
  under the same base flags, so the result line is still there to judge
  by. The event text is rendered from the wake's own events — the ids
  the ledger already holds, not a re-derived summary.
- The turn is judged by the result line (``is_error``, ``num_turns``,
  ``usage``) and the exit status, never by grepping prose. The line
  lands on the session's turns ledger, one record per turn.
- A turn that exits non-zero, prints no result line, reports
  ``is_error`` or hits its time limit is retried once with the same
  event — the event is not consumed until a turn carrying it succeeds —
  and a second failure raises the anomaly ``supervisor turn failed
  twice``.
- ``relaunch`` of a headless session is not a new process shape: it is
  a wake carrying the event ``you were relaunched, read your
  checkpoint``.

Nothing here touches a real systemd unit unless the caller runs the
default spawn: the outer argv is built by pure functions the tests
assert on structurally, and every runner takes a ``spawn`` double. The
tests drive the script itself for real with a fake ``claude`` on PATH
that records its argv and prints a result line — no test in this task
starts a real vendor process.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from . import caller, entities, ids, paths, store, wake
from . import mcp as mcp_module
from .pools import _common

#: Raised on the anomalies ledger when two turns carrying one wake die.
HEADLESS_ANOMALY = "supervisor turn failed twice"
#: The wake a headless ``relaunch`` carries. It is a ``told`` event (the
#: only reason that carries a sentence), so the vocabulary is unchanged.
RELAUNCH_TEXT = "you were relaunched, read your checkpoint"
#: ``[launch] turn_timeout`` when the configuration names none.
DEFAULT_TURN_TIMEOUT_TEXT = "15m"
DEFAULT_TURN_TIMEOUT_SECONDS = 15 * 60
#: Model a headless turn runs on when the caller names none: the same
#: one an interactive supervisor is summoned with.
SUPERVISOR_MODEL_FALLBACK = "claude-opus-5"


# --------------------------------------------------------------------------
# Configuration: `[launch] headless` and `[launch] turn_timeout`.
# --------------------------------------------------------------------------


def _launch_table() -> dict:
    """The merged ``[launch]`` table: packaged defaults, user file over.

    Read the way the launcher layers every other configured value: the
    packaged default answers where the user's file is silent, and a file
    that does not parse means the defaults, never a crash.
    """
    from . import launch as launch_module

    merged: dict = {}
    for source in (launch_module._packaged_defaults,
                   launch_module._user_config):
        try:
            data = source()
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        table = data.get("launch")
        if isinstance(table, dict):
            merged.update(table)
    return merged


def headless_default() -> bool:
    """Whether launches default to headless: ``headless = true``.

    Interactive launch stays the default until the owner flips this in
    ``[launch]``; the flag switches the default, it does not remove the
    interactive shape (``--no-headless`` still summons a window).
    """
    value = _launch_table().get("headless", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "on")
    return False


def turn_timeout_text() -> str:
    """The configured turn timeout (``[launch] turn_timeout``).

    A ``20m``-style string like every other timeout in this runtime, or
    seconds as a number. Anything misshapen answers the default rather
    than inventing a limit or refusing a launch.
    """
    value = _launch_table().get("turn_timeout", DEFAULT_TURN_TIMEOUT_TEXT)
    if isinstance(value, bool):
        return DEFAULT_TURN_TIMEOUT_TEXT
    if isinstance(value, (int, float)) and value > 0:
        return f"{int(value)}s"
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_TURN_TIMEOUT_TEXT


def turn_timeout_seconds(text: str | None = None) -> int:
    """Seconds for the turn timeout, defaulting to fifteen minutes."""
    parsed = _common.timeout_seconds(text or turn_timeout_text())
    if parsed is None or parsed <= 0:
        return DEFAULT_TURN_TIMEOUT_SECONDS
    return parsed


def resolve_launch_headless(args: Any, problems: list[str]) -> bool:
    """Whether this launch is headless: flags first, config default last.

    ``--headless`` forces it, ``--no-headless`` forces the window, and
    with neither the ``[launch] headless`` flag decides. Both flags at
    once is a refusal naming both.
    """
    want = bool(getattr(args, "headless", False))
    nope = bool(getattr(args, "no_headless", False))
    if want and nope:
        problems.append(
            "pass only one of --headless and --no-headless; "
            "one summons no window and the other summons one")
        return False
    if want:
        return True
    if nope:
        return False
    return headless_default()


# --------------------------------------------------------------------------
# Argument shapes: one `claude -p` process per turn.
# --------------------------------------------------------------------------


def _supervisor_model() -> str:
    from . import launch as launch_module

    model = getattr(launch_module, "SUPERVISOR_MODEL", None)
    return model if isinstance(model, str) and model \
        else SUPERVISOR_MODEL_FALLBACK


def first_turn_vendor_argv(*, prompt_text: str, mcp_config: Path | str,
                           model: str | None = None) -> list[str]:
    """The first turn: the role prompt as the prompt, stream-json out."""
    return [
        "claude",
        "-p",
        prompt_text,
        "--model",
        model or _supervisor_model(),
        "--dangerously-skip-permissions",
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def resume_turn_vendor_argv(*, vendor_id: str, event_text: str,
                            mcp_config: Path | str,
                            model: str | None = None) -> list[str]:
    """A later turn: resume the recorded conversation with the event text.

    The base flags are the first turn's — the MCP config for the tools,
    stream-json for the result line the turn is judged by — with
    ``--resume`` and the event text up front, in the order the brief
    names.
    """
    return [
        "claude",
        "-p",
        "--resume",
        vendor_id,
        event_text,
        "--model",
        model or _supervisor_model(),
        "--dangerously-skip-permissions",
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def turn_outer_argv(session_id: str, script_path: Path | str, *,
                    repo: str | None = None,
                    timeout_text: str | None = None) -> list[str]:
    """The transient unit one turn runs as.

    ``RuntimeMaxSec`` is the turn timeout in seconds — the unit enforces
    the timeout beside the launcher's own kill. ``--pipe`` keeps the
    launcher in the foreground until the turn ends, so the exit status
    read back is the turn's and not the spawner's, and connects the
    unit's stdout to it: under ``--wait`` the stream goes to the journal
    instead, and the vendor session id and result line the turn is
    judged by never reach the reader at all.
    """
    secs = turn_timeout_seconds(timeout_text)
    argv = [
        "systemd-run",
        "--user",
        f"--unit={_common.unit_name(session_id)}",
    ]
    if repo:
        argv.append(f"--working-directory={repo}")
    argv.append(f"--property=RuntimeMaxSec={secs}")
    argv.append("--service-type=exec")
    argv.append("--pipe")
    return [*argv, "bash", str(script_path)]


def package_export() -> str:
    """Carry this checkout's package into the turn's script.

    The run script names the package of the checkout the launch came
    from (rul-ym3xjam): ``PYTHONPATH`` puts this checkout's ``src``
    first, so the MCP server the turn's ``mcp.json`` summons runs this
    branch's verbs. The launcher's own ``PYTHONPATH`` is kept behind
    it, never replaced. Empty outside a checkout (an installed wheel).
    """
    src = mcp_module.checkout_src()
    if src is None:
        return ""
    safe = str(src).replace("\\", "\\\\").replace('"', '\\"')
    return f'export PYTHONPATH="{safe}${{PYTHONPATH:+:$PYTHONPATH}}"\n'


def turn_script_inner(vendor_argv: list[str]) -> str:
    """The turn script's body: this checkout's package, then the vendor."""
    return package_export() + " ".join(
        shlex.quote(part) for part in vendor_argv) + "\n"


def render_event_text(events: list[dict]) -> str:
    """The prompt of a wake turn, from the wake's own events.

    Each event contributes its ledger id and its reason plus the ids
    the reason names — the wake the ledger already holds, not a
    re-derived summary. A ``told`` event carries the foreman's sentence
    verbatim.
    """
    lines = []
    for event in events:
        if not isinstance(event, dict):
            continue
        eid = event.get("id") or "(no id)"
        reason = event.get("reason") or "(no reason)"
        named = " ".join(
            f"{key}={event[key]}"
            for key in ("job", "front", "task", "inbox", "ruling",
                        "rule", "from", "text")
            if isinstance(event.get(key), str) and event.get(key))
        lines.append(f"- [{eid}] {reason}"
                     + (f" ({named})" if named else ""))
    return ("Act on these wake events, in order:\n" + "\n".join(lines) + "\n"
            if lines else "(no wake events)")


# --------------------------------------------------------------------------
# The stream: the vendor session id and the result line.
# --------------------------------------------------------------------------


def _json_lines(stream: str) -> list[dict]:
    """Every JSON object the stream carries, in order.

    A malformed line is skipped, never fatal: usage probes and partial
    writes share the log with the transcript.
    """
    found = []
    for line in (stream or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    return found


def parse_vendor_session_id(stream: str) -> str | None:
    """The first ``session_id`` the stream names, or None.

    The first turn runs without one, so the stream is where the
    conversation's id is learned; every later turn resumes it.
    """
    for obj in _json_lines(stream):
        value = obj.get("session_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_result(stream: str) -> dict | None:
    """The turn's result line, or None when the stream carries none.

    The last ``{"type": "result"}`` object wins: ``is_error``,
    ``num_turns`` and ``usage`` as that line reports them. A turn with
    no result line cannot be judged and reads as failed.
    """
    result: dict | None = None
    for obj in _json_lines(stream):
        if obj.get("type") != "result":
            continue
        result = {
            "is_error": bool(obj.get("is_error", False)),
            "num_turns": obj.get("num_turns")
            if isinstance(obj.get("num_turns"), int)
            and not isinstance(obj.get("num_turns"), bool) else None,
            "usage": obj.get("usage")
            if isinstance(obj.get("usage"), dict) else None,
        }
    return result


# --------------------------------------------------------------------------
# The session's turns ledger: one record per turn.
# --------------------------------------------------------------------------


def append_turn(session_id: str, record: dict) -> dict:
    """Append one turn's record. Returns the line as written."""
    entry = dict(record)
    entry.setdefault("id", ids.mint("turn"))
    entry.setdefault("session", session_id)
    return store.append_ledger(paths.session_turns_path(session_id), entry)


def read_turns(session_id: str) -> list[dict]:
    """Every turn record, in the order the turns ran."""
    try:
        return store.read_ledger(paths.session_turns_path(session_id))
    except OSError:
        return []


def read_vendor_session(session_id: str) -> str | None:
    """The recorded vendor conversation, or None before the first turn."""
    try:
        text = (paths.session_dir(session_id) / "vendor-session"
                ).read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def record_vendor_session(session_id: str, vendor_id: str) -> None:
    """Keep the vendor id beside the session and on its roster record."""
    path = paths.session_dir(session_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "vendor-session").write_text(vendor_id + "\n",
                                         encoding="utf-8")
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _roster_update(roster, session_id,
                                      vendor_session=vendor_id),
        default={"sessions": {}},
    )


def _roster_update(roster: Any, session_id: str, **fields: Any) -> Any:
    if not isinstance(roster, dict):
        roster = {"sessions": {}}
    sessions = roster.get("sessions")
    if not isinstance(sessions, dict):
        sessions = roster["sessions"] = {}
    entry = sessions.setdefault(session_id, {"id": session_id})
    if isinstance(entry, dict):
        entry.update(fields)
    return roster


def raise_turn_anomaly(session_id: str, event_ids: list[str],
                       exits: list[Any]) -> dict:
    """The second failure of one wake: ``supervisor turn failed twice``."""
    now = store.utcnow_iso()
    record = entities.Anomaly(
        kind=HEADLESS_ANOMALY,
        subject=session_id,
        since=now,
        detail=(f"two turns carrying wake event(s) "
                f"{', '.join(event_ids) or '(none)'} failed "
                f"(exits {', '.join(str(exit) for exit in exits)}); "
                f"the events stay queued"),
    ).to_dict()
    return store.append_ledger(paths.anomalies_path(), record)


def relaunch_wake(session_id: str, by: str | None = None) -> dict:
    """Queue a headless ``relaunch``: a wake, not a new process shape."""
    return wake.append_event(session_id, "told", text=RELAUNCH_TEXT,
                             from_=by or caller.OWNER)


# --------------------------------------------------------------------------
# Running: one turn, the first turn, one wake.
# --------------------------------------------------------------------------


def unit_of(argv: list[str]) -> str | None:
    """The transient unit an outer argv names, or None."""
    for part in argv:
        if isinstance(part, str) and part.startswith("--unit="):
            return part.split("=", 1)[1]
    return None


def free_unit_name(unit: str) -> None:
    """Let a failed turn's unit name be used again by the next turn.

    A transient unit that exited non-zero stays loaded in its failed
    state, and ``systemd-run`` refuses to start another under the same
    name: "was already loaded or has a fragment file". The name is
    stable on purpose — ``foreman kill`` stops a session by it — so the
    retry cannot dodge the collision by inventing a fresh name; it
    clears the corpse first. Best effort: a name that was never used
    answers "not loaded", which is the state we wanted anyway.
    """
    try:
        subprocess.run(
            ["systemctl", "--user", "reset-failed", unit],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _default_spawn(argv: list[str], *, timeout_s: float) -> Any:
    """The real door: free the unit name, then run the turn.

    Freeing lives here and not in :func:`run_turn` so that a test which
    substitutes this function never reaches systemd (rul-tx35izr), and
    every real turn does.
    """
    unit = unit_of(argv)
    if unit:
        free_unit_name(unit)
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_s,
    )


def run_turn(*, session_id: str, vendor_argv: list[str],
             repo: str | None = None,
             timeout_text: str | None = None,
             script_path: Path | str | None = None,
             log_path: Path | str | None = None,
             spawn: Any = None,
             attempt: int = 1,
             event_ids: list[str] | tuple[str, ...] = (),
             kind: str = "wake") -> dict:
    """Run one turn and record it. Returns the outcome.

    Writes the turn's script (the world plus this checkout's package,
    then the vendor command), runs it under the transient unit, judges
    the turn by its result line and its exit status, appends one record
    to the session's turns ledger and the stream to its log. ``spawn``
    is the ``subprocess.run`` to use, taken as a parameter so tests
    never touch a systemd unit: it receives the outer argv and a
    ``timeout_s``, and answers with ``.returncode`` and ``.stdout``.
    A spawn timeout reads as a turn that hit its time limit.
    """
    resolved_timeout = timeout_text or turn_timeout_text()
    secs = turn_timeout_seconds(resolved_timeout)
    script = Path(script_path) if script_path is not None \
        else paths.session_dir(session_id) / "run.sh"
    _common.write_worker_script(script, turn_script_inner(vendor_argv))
    outer = turn_outer_argv(session_id, script, repo=repo,
                            timeout_text=resolved_timeout)
    run = spawn if spawn is not None else _default_spawn
    timed_out = False
    exit_code: Any = None
    stream = ""
    try:
        proc = run(outer, timeout_s=secs + 120)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        partial = exc.stdout
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        stream = partial if isinstance(partial, str) else ""
    else:
        exit_code = getattr(proc, "returncode", None)
        out = getattr(proc, "stdout", "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        stream = out if isinstance(out, str) else ""
    vendor_session = parse_vendor_session_id(stream)
    result = parse_result(stream)
    ok = (not timed_out and exit_code == 0 and result is not None
          and not result.get("is_error", False))
    turn = append_turn(session_id, {
        "kind": kind,
        "attempt": attempt,
        "events": list(event_ids),
        "vendor_session": vendor_session,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "is_error": result.get("is_error") if result else None,
        "num_turns": result.get("num_turns") if result else None,
        "usage": result.get("usage") if result else None,
        "timeout": resolved_timeout,
    })
    try:
        log = Path(log_path) if log_path is not None \
            else paths.session_log_path(session_id)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(stream)
            if stream and not stream.endswith("\n"):
                handle.write("\n")
    except OSError:
        pass
    return {
        "ok": ok,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "stream": stream,
        "vendor_session": vendor_session,
        "result": result,
        "turn_id": turn.get("id"),
        "outer_argv": outer,
    }


def _ensure_mcp_config(session_id: str) -> Path:
    path = mcp_module.mcp_config_path(session_id)
    if not path.exists():
        return mcp_module.write_mcp_config(session_id)
    return path


def run_first_turn(session_id: str, *, prompt_text: str,
                   repo: str | None = None,
                   mcp_config: Path | str | None = None,
                   model: str | None = None,
                   timeout_text: str | None = None,
                   spawn: Any = None) -> dict:
    """Run the summoning turn(s): the role prompt as the prompt.

    One attempt, retried once with the same prompt when it dies — the
    first turn carries no wake event, so there is nothing to consume,
    only a vendor session id to learn. Returns the attempts, whether
    any succeeded, and the first vendor id any attempt reported.
    """
    vendor_argv = first_turn_vendor_argv(
        prompt_text=prompt_text,
        mcp_config=mcp_config if mcp_config is not None
        else _ensure_mcp_config(session_id),
        model=model)
    attempts = [run_turn(
        session_id=session_id, vendor_argv=vendor_argv, repo=repo,
        timeout_text=timeout_text, spawn=spawn, attempt=1, kind="first")]
    if not attempts[0]["ok"]:
        attempts.append(run_turn(
            session_id=session_id, vendor_argv=vendor_argv, repo=repo,
            timeout_text=timeout_text, spawn=spawn, attempt=2, kind="first"))
    vendor_session: str | None = None
    for attempt in attempts:
        if attempt["vendor_session"]:
            vendor_session = attempt["vendor_session"]
            break
    return {
        "attempts": attempts,
        "ok": any(attempt["ok"] for attempt in attempts),
        "vendor_session": vendor_session,
    }


def run_wake(session_id: str, *, spawn: Any = None,
             timeout_text: str | None = None,
             model: str | None = None) -> dict:
    """Carry one wake: the queued events, one turn, retried once.

    A queued event waits while a turn of this session is running, then
    one wake carries every event queued during it. The wake is consumed
    (each event stamped delivered) only when a turn carrying it
    succeeds; a failed turn leaves every event queued and a second
    failure raises ``supervisor turn failed twice``. A session whose
    vendor conversation was never learned runs the first-turn shape —
    the role prompt as the prompt — and records the id off its stream.
    No queued events means no turn at all.
    """
    try:
        sessions = caller.read_roster().get("sessions", {})
    except OSError:
        sessions = {}
    record = sessions.get(session_id) if isinstance(sessions, dict) else None
    if not isinstance(record, dict):
        return {"status": "unknown-session", "attempts": []}
    if (record.get("state") or "") not in ("starting", "running", "stalled"):
        return {"status": "not-running", "attempts": []}
    if wake.turn_running(session_id):
        return {"status": "deferred", "attempts": []}
    pending = wake.pending_events(session_id)
    if not pending:
        return {"status": "no-wake", "attempts": []}
    event_ids = [str(event.get("id")) for event in pending
                 if isinstance(event, dict) and event.get("id")]
    vendor_id = read_vendor_session(session_id) \
        or record.get("vendor_session")
    vendor_id = vendor_id if isinstance(vendor_id, str) and vendor_id \
        else None
    mcp_config = _ensure_mcp_config(session_id)
    repo = record.get("worktree")
    repo = repo if isinstance(repo, str) and repo else None
    if vendor_id is not None:
        vendor_argv = resume_turn_vendor_argv(
            vendor_id=vendor_id,
            event_text=render_event_text(pending),
            mcp_config=mcp_config, model=model)
        kind = "wake"
    else:
        from . import launch as launch_module

        try:
            prompt_text = (
                paths.session_dir(session_id)
                / launch_module.ROLE_PROMPT_FILE
            ).read_text(encoding="utf-8")
        except OSError:
            return {"status": "no-prompt", "attempts": []}
        vendor_argv = first_turn_vendor_argv(
            prompt_text=prompt_text, mcp_config=mcp_config, model=model)
        kind = "first"
    wake.turn_started(session_id)
    try:
        attempts = [run_turn(
            session_id=session_id, vendor_argv=vendor_argv, repo=repo,
            timeout_text=timeout_text, spawn=spawn, attempt=1,
            event_ids=event_ids, kind=kind)]
        if not attempts[0]["ok"]:
            attempts.append(run_turn(
                session_id=session_id, vendor_argv=vendor_argv, repo=repo,
                timeout_text=timeout_text, spawn=spawn, attempt=2,
                event_ids=event_ids, kind=kind))
        ok = any(attempt["ok"] for attempt in attempts)
        anomaly = False
        if ok:
            stamped = store.utcnow_iso()
            for event in pending:
                store.append_ledger(
                    paths.session_events_path(session_id),
                    dict(event, delivered_at=stamped))
            if vendor_id is None:
                for attempt in attempts:
                    if attempt["vendor_session"]:
                        record_vendor_session(
                            session_id, attempt["vendor_session"])
                        break
        else:
            raise_turn_anomaly(
                session_id, event_ids,
                [attempt["exit_code"] if not attempt["timed_out"]
                 else "timeout" for attempt in attempts])
            anomaly = True
        return {
            "status": "ok" if ok else "failed",
            "wake": pending,
            "attempts": attempts,
            "anomaly": anomaly,
        }
    finally:
        wake.turn_ended(session_id)
