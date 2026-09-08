"""`foreman launch <role> <pool> <spec>`: summon one worker.

In order: refuse while the frozen file exists and refuse callers outside
the foreman and supervisor roles; mint a session id; create a git worktree
on a new branch from the base branch; create the per-session directory
holding a log file that is never reused; resolve the timeout from the
pool's default unless ``--timeout`` overrides; write ``FOREMAN-JOB.md``
(with the role prompt embedded, so the worker actually receives it) and
``FOREMAN-ROLE.md`` into the worktree, refusing symlinks; record the
session on the roster snapshot under one lock; start the process through
the pool's adapter and move the record to running with its pid and
process group; print the session id, the worktree, the log path and the
pid. A failure before the spawn removes the worktree and branch; a failed
spawn is recorded as failed, never running; a dry run records nothing.

Every path written into the injected files is absolute: a worker process
runs from its own home directory, not from its worktree, so a relative
path there is a dead launch. A relative worktree, spec or log path is
refused before anything is created, and every violated field is named at
once.

`foreman launch supervisor <front>` is the second argument shape, beside
the worker one and reusing every part of it: a supervisor has no spec file
and no worktree, so it takes the front's name where a worker names its
pool, and works in the repository on the front's branch. Its identity is
minted before its process exists — session id, role prompt and vendor
session id are written first, the window opens second — so it is on the
roster from the moment it starts and is missed by the collector when it
goes quiet. `register` puts a session Foreman never started (a
hand-started supervisor, the orchestrator) on the same roster with the
same process identity; `relaunch` replaces a supervisor with a fresh
prompt carrying its predecessor's last checkpoint, resuming the same
vendor conversation.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

from . import caller, fronts, ids, paths, procs, store
from .caller import FOREMAN, SUPERVISOR
from .cli import subcommand
from . import entities
from .entities import JOB_KINDS, JOB_ROLES, SESSION_ROLES, Session
from .pools import LaunchContext, get as get_pool
from .pools import _common
from .pools._common import LAUNCH_EFFORTS as EFFORTS

JOB_FILE = "FOREMAN-JOB.md"
ROLE_FILE = "FOREMAN-ROLE.md"
PAGE_LINES = 120

VERIFY_HINTS = (
    "pytest",
    "python -m",
    "uv run",
    "npm test",
    "npm run",
    "cargo test",
    "go test",
    "make test",
    "make check",
    "foreman-verify",
    "gh pr checks",
)

#: The owner's own debugging switch for a worker window, and the only way
#: one can be opened. Owner ruling: worker and reviewer agents never show a
#: command line on screen; only the foreman and supervisors are visible as
#: command lines. A supervisor asking for a window is refused by name, so
#: the rule holds by construction and not by a sentence in a spec.
WORKER_WINDOW_ENV = "FOREMAN_WORKER_WINDOW"

TIMEOUT_RE = re.compile(r"(?:\d+[smhd])+$")
UNIT_RANGE_RE = re.compile(r"(\d+)-(\d+)$")


def add_launch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "role",
        help="worker role (one of: " + ", ".join(JOB_ROLES)
        + "), or 'supervisor' for the second shape: launch supervisor <front>")
    parser.add_argument(
        "pool", help="pool to start the worker through; the front name when "
                     "the role is 'supervisor'")
    parser.add_argument("spec", nargs="?", default=None,
                        help="absolute path to the supervisor's spec file "
                             "(a supervisor launch takes none)")
    parser.add_argument("--workspace", default=None,
                        help="workspace to open a supervisor's window on")
    parser.add_argument("--front", default=None, help="front name")
    parser.add_argument("--task", default=None, help="task id or title")
    parser.add_argument("--job", default=None, help="job id this session runs")
    parser.add_argument("--kind", default="implement", choices=JOB_KINDS)
    parser.add_argument("--branch", default=None, help="new branch for the worktree")
    parser.add_argument("--base", default=None, help="base branch (merge target)")
    parser.add_argument("--repo", default=None, help="repo to create the worktree from")
    parser.add_argument("--worktree", default=None, help="absolute worktree path")
    parser.add_argument("--log", default=None, help="absolute log path")
    parser.add_argument("--timeout", default=None, help="e.g. 20m (pool default otherwise)")
    parser.add_argument("--units", default=None, help="unit range, e.g. 3-7")
    parser.add_argument("--effort", default="high", choices=EFFORTS)
    parser.add_argument("--scope", default=None, help="task scope text")
    parser.add_argument("--window", action="store_true",
                        help="owner debugging only: open this worker in a "
                             "terminal window. Requires "
                             + WORKER_WINDOW_ENV + "=1 and refuses any caller "
                             "but the owner; workers and reviewers are never "
                             "on screen.")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except start the process; print the command")


class Refused(Exception):
    """A launch refusal: str lists every violated field at once."""


def parse_units(text: str) -> list[int]:
    parts = [piece.strip() for piece in text.split(",") if piece.strip()]
    if not parts:
        raise ValueError("empty units")
    units: list[int] = []
    for piece in parts:
        match = UNIT_RANGE_RE.fullmatch(piece)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start > end or start < 1:
                raise ValueError(f"bad range {piece!r}")
            units.extend(range(start, end + 1))
        elif piece.isdigit() and int(piece) >= 1:
            units.append(int(piece))
        else:
            raise ValueError(f"bad units {piece!r}")
    return units


def spec_problems(text: str) -> list[str]:
    """Both spec refusals, so both are named at once when both are wrong."""
    problems = []
    lines = text.splitlines()
    if len(lines) > PAGE_LINES:
        problems.append(
            f"spec longer than one page: {len(lines)} lines (one page is {PAGE_LINES} lines)"
        )
    lowered = [line.lower() for line in lines]
    if not any(hint in line for line in lowered for hint in VERIFY_HINTS):
        problems.append("spec contains no verification command")
    return problems


def run_git(repo: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def default_base(repo: str) -> str:
    try:
        return run_git(repo, "symbolic-ref", "--short", "HEAD")
    except Refused:
        return "HEAD"


def read_rulings(front: str | None = None) -> list[str]:
    """Swarm rulings plus this front's own; nothing else travels.

    A worker on one front must never see another front's rulings,
    so every other scope is left out of the injected block.
    """
    texts = []
    for record in store.read_ledger(paths.rulings_path()):
        text = record.get("text")
        if not (isinstance(text, str) and text.strip()):
            continue
        if record.get("scope") != "swarm" and record.get("scope") != front:
            continue
        texts.append(text)
    return texts


def format_rulings(texts: list[str]) -> str:
    if not texts:
        return "(no rulings recorded)"
    return "\n".join(f"- {text}" for text in texts)


def lookup_scope(front: str | None, task: str | None) -> str | None:
    if not front or not task:
        return None
    for record in store.read_ledger(paths.front_tasks_path(front)):
        if record.get("id") == task or record.get("title") == task:
            scope = record.get("scope")
            if isinstance(scope, str) and scope.strip():
                return scope
    return None


def environment_block(worktree: str, branch: str, target: str, scratch: str,
                      log: str, verdict: str, timeout: str) -> str:
    return (
        f"- worktree: {worktree}\n"
        f"- branch: {branch}\n"
        f"- target: {target}\n"
        f"- scratch directory: {scratch}\n"
        f"- finish marker path: {log} "
        "(the line `### finished rc=$?` is written to this file when the worker exits)\n"
        f"- verdict path: {verdict}\n"
        f"- timeout: {timeout}"
    )


def build_job_file(job: str, kind: str, task: str, front: str, env: str,
                   rulings: str, scope: str, spec: str,
                   units_label: str | None = None,
                   *, role_text: str) -> str:
    this_job = f"units: {units_label}\n\n{spec}" if units_label else spec
    return (
        f"# Job {job} \u00b7 {kind} \u00b7 task \"{task}\" \u00b7 front {front}\n"
        "\n"
        "## Role prompt (from FOREMAN-ROLE.md, verbatim: no session starts\n"
        "without its role prompt, and the worker is given this file)\n"
        "\n"
        f"{role_text}\n"
        "\n"
        "## Environment (injected)\n"
        "\n"
        f"{env}\n"
        "\n"
        "## Rules (injected: swarm + front rulings, verbatim)\n"
        "\n"
        f"{rulings}\n"
        "\n"
        "## Task scope (verbatim from the brief)\n"
        "\n"
        f"{scope}\n"
        "\n"
        "## This job (written by the supervisor)\n"
        "\n"
        f"{this_job}\n"
    )


TEMPLATE_DIR = Path(__file__).with_name("templates")


def render_role_template(role: str, mapping: dict[str, str]) -> str:
    path = TEMPLATE_DIR / f"{role}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        known = sorted(p.stem for p in TEMPLATE_DIR.glob("*.md"))
        raise Refused(
            f"unknown role {role!r}; roles with a template: "
            + (", ".join(known) or "(none)")
        ) from None
    for key, value in mapping.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def refuse(*problems: str) -> int:
    print("foreman launch: refused", file=sys.stderr)
    for problem in problems:
        print(f"- {problem}", file=sys.stderr)
    return 1


def write_no_symlink(path: str, text: str) -> None:
    """Write launcher output, refusing to follow a symlink.

    A worktree checked out from a hostile branch can carry FOREMAN-JOB.md
    or FOREMAN-ROLE.md as a symlink; writing through it would land outside
    the worktree. O_NOFOLLOW makes the refusal atomic with the open.
    """
    if os.path.islink(path):
        raise Refused(f"refusing to write through symlink {path!r}")
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Refused(
                f"refusing to write through symlink {path!r}") from None
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def remove_launch_worktree(repo: str, branch: str, worktree: str) -> None:
    """Best-effort undo of the worktree half of a failed launch."""
    for argv in (("worktree", "remove", "--force", worktree),
                 ("branch", "-D", branch)):
        try:
            run_git(repo, *argv)
        except Refused:
            pass


def _place_session(roster, session: Session):
    if not isinstance(roster, dict):
        roster = {"sessions": {}}
    sessions = roster.get("sessions")
    if not isinstance(sessions, dict):
        sessions = roster["sessions"] = {}
    sessions[session.id or ""] = session.to_dict()
    return roster


def _move_session(roster, session_id: str, **fields):
    if not isinstance(roster, dict):
        roster = {"sessions": {}}
    sessions = roster.get("sessions")
    if not isinstance(sessions, dict):
        sessions = roster["sessions"] = {}
    entry = sessions.setdefault(session_id, {"id": session_id})
    if isinstance(entry, dict):
        entry.update(fields)
    return roster


@subcommand("launch", help="Mint a session, build its worktree, start its worker.")
def cmd_launch(args: argparse.Namespace) -> int:
    frozen = paths.frozen_path()
    if frozen.exists():
        return refuse(
            f"frozen file {frozen} exists; the launcher refuses while frozen"
        )
    me, identity_violations = caller.resolve("launch")
    caller.check_role(me, "launch", FOREMAN, SUPERVISOR,
                      violations=identity_violations)
    # The second argument shape, dispatched before the pool is resolved:
    # a supervisor names its front where a worker names its pool.
    if args.role == SUPERVISOR:
        return launch_supervisor_main(args, list(identity_violations))
    problems: list[str] = list(identity_violations)
    if args.spec is None:
        problems.append(
            "spec is required: launch <role> <pool> <spec>, "
            "or launch supervisor <front> for a supervisor")

    try:
        adapter = get_pool(args.pool)
    except ValueError as exc:
        return refuse(str(exc))
    if args.role not in JOB_ROLES:
        return refuse(
            f"unknown role {args.role!r}; known roles: " + ", ".join(JOB_ROLES)
        )
    if args.timeout is not None and not TIMEOUT_RE.fullmatch(args.timeout):
        problems.append(
            f"bad timeout {args.timeout!r}; use a number with a unit, e.g. 20m"
        )
    try:
        units = parse_units(args.units) if args.units else []
    except ValueError as exc:
        problems.append(str(exc))
        units = []

    if getattr(args, "window", False):
        if me is not None and me.role != caller.OWNER:
            problems.append(
                f"role '{me.role}' may not ask for a worker window; workers "
                "and reviewers are never on screen, and summoning a "
                "supervisor is the only launch that opens one")
        elif os.environ.get(WORKER_WINDOW_ENV) != "1":
            problems.append(
                f"a worker window is the owner's debugging switch: set "
                f"{WORKER_WINDOW_ENV}=1 to open one")

    for label, value in (("worktree", args.worktree), ("spec", args.spec),
                         ("log", args.log)):
        if value is not None and not os.path.isabs(value):
            problems.append(f"relative {label} path {value!r}; give an absolute path")

    repo = os.path.abspath(args.repo or os.getcwd())
    if not os.path.isdir(repo):
        problems.append(f"repo {repo!r} is not a directory")

    spec_text: str | None = None
    if args.spec is not None and os.path.isabs(args.spec):
        try:
            spec_text = Path(args.spec).read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"cannot read spec {args.spec!r}: {exc.strerror or exc}")
    if spec_text is not None:
        problems.extend(spec_problems(spec_text))

    if problems:
        return refuse(*problems)
    assert spec_text is not None

    session_id = ids.mint("session")
    branch = args.branch or f"foreman/{session_id}"
    target = args.base or default_base(repo)
    worktree = os.path.abspath(
        # Worktrees live under the state directory, not beside the repo:
        # a swarm must not litter the directory its repository sits in.
        args.worktree or str(paths.state_dir() / "worktrees" / session_id)
    )
    log_path = os.path.abspath(
        args.log or str(paths.session_log_path(session_id)))
    pid_path = os.path.abspath(str(paths.session_pid_path(session_id)))
    verdict_path = os.path.abspath(str(paths.session_verdict_path(session_id)))
    scratch_dir = os.path.abspath(str(paths.session_scratch_dir(session_id)))
    timeout = args.timeout or adapter.timeout_default

    if os.path.exists(log_path):
        return refuse(f"log file {log_path!r} already exists; logs are never reused")

    try:
        run_git(repo, "worktree", "add", "-b", branch, worktree, target)
    except Refused as exc:
        return refuse(str(exc))

    try:
        session_dir = paths.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        Path(scratch_dir).mkdir(parents=True, exist_ok=True)
        Path(log_path).touch(exist_ok=False)

        rulings = read_rulings(args.front)
        rulings_block = format_rulings(rulings)
        scope = (
            args.scope
            or lookup_scope(args.front, args.task)
            or "(no task scope recorded)"
        )
        env = environment_block(worktree, branch, target, scratch_dir,
                                log_path, verdict_path, timeout)
        job_label = args.job or "(none)"
        task_label = args.task or "(none)"
        front_label = args.front or "(none)"
        job_path = os.path.join(worktree, JOB_FILE)
        role_path = os.path.join(worktree, ROLE_FILE)
        # The role prompt is rendered first: no session starts without one,
        # and the worker is given the job file, so it is embedded there.
        role_text = render_role_template(args.role, {
            "role": args.role,
            "front": front_label,
            "supervisor": args.front or "(none)",
            "session_id": session_id,
            "pool": args.pool,
            "model": adapter.model,
            "worktree": worktree,
            "branch": branch,
            "timeout": timeout,
            "log_path": log_path,
            "verdict_path": verdict_path,
            "goal": scope,
            "rulings": rulings_block,
            "environment": env,
        })
        write_no_symlink(
            job_path,
            build_job_file(job_label, args.kind, task_label, front_label,
                           env, rulings_block, scope, spec_text, args.units,
                           role_text=role_text),
        )
        write_no_symlink(role_path, role_text)

        # The job id is minted before the session so both records name
        # each other: without the job on the session the collector cannot
        # tell which job a finish marker belongs to, and a finished job
        # stays "running" on the screen forever.
        job_id = args.job or (ids.mint("job") if args.front else None)
        starting = Session(
            id=session_id,
            role=args.role,
            pool=args.pool,
            model=adapter.model,
            front=args.front,
            job=job_id,
            pid=None,
            pgid=None,
            worktree=worktree,
            log=log_path,
            timeout=timeout,
            launched_by=os.environ.get("FOREMAN_SESSION"),
            started_at=store.utcnow_iso(),
            state="starting",
        )
        ctx = LaunchContext(
            session=starting,
            worktree=Path(worktree),
            job_path=Path(job_path),
            role_path=Path(role_path),
            log_path=Path(log_path),
            pid_path=Path(pid_path),
            verdict_path=Path(verdict_path),
            scratch_dir=Path(scratch_dir),
            branch=branch,
            target=target,
            timeout=timeout,
            effort=args.effort,
            units=tuple(units),
            kind=args.kind,
            window=bool(getattr(args, "window", False)),
        )
        # A dry run starts nothing, so it records nothing: the roster is
        # left as found. A real launch records the session before the
        # spawn, so a dead spawn still leaves a record, never an orphan.
        if not args.dry_run:
            store.update_snapshot(
                paths.roster_path(),
                lambda roster: _place_session(roster, starting),
                default={"sessions": {}},
            )
    except Refused as exc:
        remove_launch_worktree(repo, branch, worktree)
        return refuse(str(exc))
    except OSError as exc:
        remove_launch_worktree(repo, branch, worktree)
        return refuse(f"cannot write launch files: {exc.strerror or exc}")

    pid: int | None = None
    if not args.dry_run:
        try:
            pid = adapter.launch(ctx)
        except Exception as exc:  # noqa: BLE001 - a dead spawn is a refusal
            store.update_snapshot(
                paths.roster_path(),
                lambda roster: _move_session(
                    roster, session_id, state="failed", pid=None, pgid=None),
                default={"sessions": {}},
            )
            return refuse(f"pool {args.pool} failed to start the process: {exc}")
        # The wrapper runs under setsid as a process group leader, so its
        # pid is its pgid by construction; the group is what a later kill
        # signals to reach the vendor process. The starttime beside the pid
        # is the process identity: the collector requires both to match
        # before believing or killing, so pid reuse cannot hide an intruder
        # or aim a kill at an unrelated process.
        starttime = procs.proc_starttime(pid)
        store.update_snapshot(
            paths.roster_path(),
            lambda roster: _move_session(
                roster, session_id, state="running", pid=pid, pgid=pid,
                pid_starttime=starttime),
            default={"sessions": {}},
        )
        # In v0 the launcher is the only thing that makes a job exist, so
        # it records the one it just started: without that line the owner
        # sees a session counted in the header and nothing on the screen
        # saying what is running, which is the whole point of the screen.
        if args.front:
            job = entities.Job(
                id=job_id, task=args.task,
                kind=args.kind, role=args.role,
                spec_path=os.path.abspath(args.spec), session=session_id,
                worktree=worktree, branch=branch, log=log_path,
                timeout=timeout, state="running",
                started_at=store.utcnow_iso(),
            )
            store.append_ledger(paths.front_jobs_path(args.front),
                                job.to_dict(), session_id=session_id)
    command = adapter.command_str(ctx)

    print(f"session: {session_id}")
    print(f"worktree: {worktree}")
    print(f"log: {log_path}")
    print(f"pid: {pid if pid is not None else '(not started --dry-run)'}")
    print(f"command: {command}")
    return 0


cmd_launch.add_arguments = add_launch_arguments  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# The second shape: summoning a supervisor.
#
# A supervisor has no spec file and no worktree. It works in the repository
# on the front's branch, in a terminal window, interactively, and it is on
# the roster from the moment it starts because its identity is minted before
# its process exists: the session id, the role prompt and the vendor session
# id are all written first, then the window opens. A session nobody minted,
# holding no slot, invisible to the screen, is exactly the failure this ends.
# --------------------------------------------------------------------------

SUPERVISOR_POOL = "opus"
SUPERVISOR_MODEL = "claude-opus-5"
ROLE_PROMPT_FILE = "role-prompt.md"
#: The vendor's own session id (a uuid) lives beside the session, not on the
#: Session record: Foreman mints the roster id, the vendor mints its own, and
#: ``relaunch`` reads this file back to resume the same conversation.
VENDOR_SESSION_FILE = "vendor-session"
RUN_SCRIPT_FILE = "run.sh"
#: Where the vendor's first-launch answers live. Overridable so a test never
#: touches the machine's real one.
VENDOR_CONFIG_ENV = "FOREMAN_VENDOR_CONFIG"
#: Autocompact earlier than the default: a supervisor is a long conversation
#: and a compaction that arrives at the ceiling loses the front's history.
AUTOCOMPACT = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=40"
#: The window closes itself after this many seconds, so a finished session
#: leaves the owner's workspace clean without closing on its own last words.
WINDOW_LINGER_SECONDS = 120
WORKSPACE_RE = re.compile(r"\d+")

#: The verbs a supervisor may call in this version, each with why it exists.
#: One list, rendered into the role prompt: a supervisor told about a verb
#: that does not exist flounders, and one it is never told about is unused.
SUPERVISOR_VERBS = (
    ("foreman checkpoint --doing \"…\" --next \"…\"",
     "declare what you are doing and what comes next; the only thing that "
     "puts you on the screen alive"),
    ("foreman ask \"…\" --kind money|irreversible|scope|error",
     "put a question to the Foreman; those four kinds and no others"),
    ("foreman launch <role> <pool> <spec>",
     "start one worker on one job spec, inside your allocation"),
    ("foreman status",
     "read the swarm the way the owner reads it"),
    ("foreman front list",
     "the fronts and what each waits for"),
    ("foreman rule list",
     "the rulings ledger, in full"),
    ("foreman rule ack <ruling>",
     "acknowledge a ruling you have read and will work under"),
)
#: Named so a supervisor knows these exist by design and does not invent
#: them: they are the design's supervisor row that this version has not
#: shipped yet.
SUPERVISOR_VERBS_UNSHIPPED = (
    "task ready|built", "job plan|order|verify|fail", "evidence", "finding",
    "measure", "merge request", "front done",
)


def vendor_config_path() -> Path:
    override = os.environ.get(VENDOR_CONFIG_ENV, "").strip()
    return Path(override) if override else Path.home() / ".claude.json"


def pre_answer_first_launch_dialogs(directory: str) -> None:
    """Answer both first-launch dialogs before the window opens.

    A window sitting on a question nobody will click is a dead launch: the
    session exists on the roster, burns its workspace, and never starts. The
    bypass-permissions warning and the trust-this-folder question for the
    repository both live in the vendor's config file. A config that exists
    but cannot be read is left exactly as it is: never clobbered.
    """
    path = vendor_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    data["bypassPermissionsModeAccepted"] = True
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return
    entry = projects.setdefault(directory, {})
    if not isinstance(entry, dict):
        return
    entry["hasTrustDialogAccepted"] = True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        store.write_snapshot(path, data)
    except OSError:
        return


def supervisor_inner_command(*, pid_path: Path, session_id: str, repo: str,
                             role_prompt: Path, vendor_id: str,
                             resume: bool) -> str:
    """The shell the window runs: pid first, then the proven vendor line.

    The pid is written as the first act and read back by the launcher, so
    the roster carries the window's own pid rather than the spawner's. The
    session id is exported because every ``foreman`` call the supervisor
    makes takes its caller from the environment; without it the CLI refuses
    its own supervisor as an unregistered writer.

    A resume passes **no positional prompt**: a ``--resume`` with one sits
    idle and never starts. The fresh prompt is on disk either way.
    """
    if resume:
        vendor = (f"{AUTOCOMPACT} claude --resume {shlex.quote(vendor_id)} "
                  f"--model {SUPERVISOR_MODEL} --dangerously-skip-permissions")
    else:
        vendor = (f"{AUTOCOMPACT} claude --session-id {shlex.quote(vendor_id)} "
                  f"--model {SUPERVISOR_MODEL} --dangerously-skip-permissions "
                  f'"$(cat {shlex.quote(str(role_prompt))})"')
    return (
        f"echo $$ > {shlex.quote(str(pid_path))}\n"
        f"export {caller.SESSION_ENV}={shlex.quote(session_id)}\n"
        f"cd {shlex.quote(repo)}\n"
        f"{vendor}\n"
        f"sleep {WINDOW_LINGER_SECONDS}\n"
    )


def supervisor_outer_argv(session_id: str, script_path: Path,
                          workspace: str | None) -> list[str]:
    """Place the window through the shipped helper; never reimplement it.

    The helper takes a name and the command to run, and reads the workspace
    from ``AGENT_WS``. There is no headless fallback here: a supervisor with
    no window is a session with nobody at the keyboard.
    """
    argv = [_common.window_launcher_or_default(), f"foreman-{session_id}",
            "bash", str(script_path)]
    if workspace is not None:
        argv = ["env", f"AGENT_WS={workspace}", *argv]
    return argv


def window_name(session_id: str) -> str:
    return f"org.agent.foreman-{session_id}"


def front_tasks_block(front: str) -> str:
    """Every task with its title, state, size and what it comes after."""
    records = store.fold_by_id(
        store.read_ledger(paths.front_tasks_path(front)))
    if not records:
        return "(this front has no tasks on the ledger)"
    lines = []
    for record in records:
        after = [entry for entry in (record.get("after") or [])
                 if isinstance(entry, str) and entry]
        lines.append(
            f"- \"{record.get('title') or '(untitled)'}\" — "
            f"{record.get('state') or 'unknown'} · "
            f"size {record.get('size', 0)} · "
            f"after {', '.join(after) if after else 'nothing'}"
        )
    return "\n".join(lines)


def allocation_block(record: dict) -> str:
    allocation = record.get("allocation")
    if not isinstance(allocation, dict) or not allocation:
        return "(the brief allocates no workers: you hold none by right)"
    return "\n".join(
        f"- {role}: none — this front may not take a {role} worker"
        if count == 0 else f"- {role}: at most {count} at once"
        for role, count in sorted(allocation.items())
    )


def verbs_block() -> str:
    lines = [f"- `{verb}` — {why}" for verb, why in SUPERVISOR_VERBS]
    lines.append(
        "- The design gives your role "
        + ", ".join(f"`{verb}`" for verb in SUPERVISOR_VERBS_UNSHIPPED)
        + " as well; this version does not ship them yet, so do not call them "
          "and do not invent a substitute."
    )
    return "\n".join(lines)


def supervisor_environment_block(*, repo: str, branch: str, session_id: str,
                                 role_prompt: str, checkpoint: str) -> str:
    return (
        f"- repository: {repo}\n"
        f"- branch: {branch}\n"
        f"- state directory: {paths.state_dir()}\n"
        f"- your session id: {session_id}\n"
        f"- {caller.SESSION_ENV}: must be set to {session_id} on every "
        f"`foreman` call. Your window exports it; a shell you open yourself "
        f"must set it, or the CLI refuses you as an unregistered writer.\n"
        f"- your role prompt: {role_prompt}\n"
        f"- your checkpoint: {checkpoint}"
    )


def predecessor_block(session_id: str | None) -> str:
    """The predecessor's last checkpoint, replayed, or the fresh-start line."""
    if not session_id:
        return ("(none: you are the first supervisor summoned for this "
                "front, so there is nothing to pick up.)")
    record = store.read_snapshot(paths.checkpoint_path(session_id),
                                 default=None)
    if not isinstance(record, dict):
        return (f"Session {session_id}, which you replace, left no "
                f"checkpoint. Read the front ledger and the brief, then "
                f"checkpoint what you find before you plan anything.")
    held = [str(item) for item in (record.get("held") or [])]
    questions = [str(item) for item in (record.get("questions") or [])]
    return (
        f"Session {session_id}, which you replace, left this. It is where you "
        f"pick up:\n\n"
        f"- doing: {record.get('doing') or '(not declared)'}\n"
        f"- next: {record.get('next') or '(not declared)'}\n"
        f"- held: {', '.join(held) if held else 'nothing'}\n"
        f"- open questions: "
        f"{', '.join(questions) if questions else 'none'}"
    )


def render_supervisor_prompt(*, front: str, record: dict, session_id: str,
                             repo: str, branch: str, role_prompt: Path,
                             predecessor: str | None) -> str:
    from .collector import DEFAULTS

    silent = float(DEFAULTS["supervisor_silent_seconds"]) / 60
    return render_role_template(SUPERVISOR, {
        "front": front,
        "session_id": session_id,
        "want": (record.get("want") or "(the brief records no want)").strip(),
        "done_when": (record.get("done_when")
                      or "(the brief records no done-when)").strip(),
        "tasks": front_tasks_block(front),
        "allocation": allocation_block(record),
        "verbs": verbs_block(),
        "silent_minutes": f"{silent:.0f}",
        "rulings": format_rulings(read_rulings(front)),
        "predecessor": predecessor_block(predecessor),
        "environment": supervisor_environment_block(
            repo=repo, branch=branch, session_id=session_id,
            role_prompt=str(role_prompt),
            checkpoint=str(paths.checkpoint_path(session_id))),
    })


def _write_vendor_session(session_id: str, vendor_id: str) -> Path:
    path = paths.session_dir(session_id) / VENDOR_SESSION_FILE
    path.write_text(vendor_id + "\n", encoding="utf-8")
    return path


def read_vendor_session(session_id: str) -> str | None:
    try:
        text = (paths.session_dir(session_id)
                / VENDOR_SESSION_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def _supervisor_session(session_id: str, front: str | None, repo: str,
                        launched_by: str | None) -> Session:
    return Session(
        id=session_id,
        role=SUPERVISOR,
        pool=SUPERVISOR_POOL,
        model=SUPERVISOR_MODEL,
        front=front,
        job=None,
        worktree=repo,
        log=str(paths.session_log_path(session_id)),
        launched_by=launched_by,
        started_at=store.utcnow_iso(),
        state="starting",
    )


def _start_supervisor(session_id: str, *, repo: str, role_prompt: Path,
                      vendor_id: str, workspace: str | None,
                      resume: bool) -> tuple[list[str], str, int | None,
                                             str | None]:
    """Write the window's script, open it, read the pid back.

    Returns the outer argv, the inner shell, the pid and a failure message;
    exactly one of the last two is set.
    """
    session_dir = paths.session_dir(session_id)
    pid_path = paths.session_pid_path(session_id)
    inner = supervisor_inner_command(
        pid_path=pid_path, session_id=session_id, repo=repo,
        role_prompt=role_prompt, vendor_id=vendor_id, resume=resume)
    argv = supervisor_outer_argv(session_id, session_dir / RUN_SCRIPT_FILE,
                                 workspace)
    pre_answer_first_launch_dialogs(repo)
    _common.write_worker_script(session_dir / RUN_SCRIPT_FILE, inner)
    try:
        pid = _common.spawn_and_wait(
            argv, pid_path=pid_path, session_id=session_id,
            popen=subprocess.Popen)
    except Exception as exc:  # noqa: BLE001 - a dead spawn is a refusal
        return argv, inner, None, str(exc)
    return argv, inner, pid, None


def _print_supervisor(session_id: str, vendor_id: str, role_prompt: Path,
                      workspace: str | None, pid: int | None,
                      argv: list[str], inner: str) -> None:
    print(f"session: {session_id}")
    print(f"vendor session: {vendor_id}")
    print(f"role prompt: {role_prompt}")
    where = (f"workspace {workspace}" if workspace is not None
             else "workspace from the window helper")
    print(f"window: {window_name(session_id)} {where}")
    print(f"pid: {pid if pid is not None else '(not started --dry-run)'}")
    print(f"command: {_common.printable_command(argv, inner)}")


def _record_running(session_id: str, pid: int) -> None:
    # The starttime beside the pid is the process identity: the collector
    # requires both to match before believing a session alive, so a later
    # process reusing the number cannot inherit this supervisor's slot.
    #
    # The group is asked for rather than assumed: unlike a headless worker,
    # whose wrapper runs under setsid and is its own group leader, this
    # shell runs inside a terminal it did not start, so its pid is not its
    # pgid and a kill aimed at the pid as a group would miss.
    starttime = procs.proc_starttime(pid)
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _move_session(roster, session_id, state="running",
                                     pid=pid, pgid=pgid,
                                     pid_starttime=starttime),
        default={"sessions": {}},
    )


def launch_supervisor_main(args: argparse.Namespace,
                           problems: list[str]) -> int:
    """`foreman launch supervisor <front> [--workspace N] [--dry-run]`."""
    front = args.pool
    workspace = args.workspace
    if workspace is not None and not WORKSPACE_RE.fullmatch(str(workspace)):
        problems.append(
            f"bad workspace {workspace!r}; a workspace is digits, e.g. 6")
    record = fronts.read_front_record(front)
    if record is None:
        problems.append(
            f"unknown front '{front}'; it has no record on the ledger "
            f"(add it with `foreman front add <dir>` first)")
    if args.spec is not None:
        problems.append(
            "a supervisor takes no spec file: it works in the repository "
            "on the front's branch")
    repo = os.path.abspath(args.repo or os.getcwd())
    if not os.path.isdir(repo):
        problems.append(f"repo {repo!r} is not a directory")
    if problems:
        return refuse(*problems)
    assert record is not None

    session_id = ids.mint("session")
    branch = args.branch or default_base(repo)
    # The vendor mints no id of its own for a fresh session, so Foreman
    # generates the uuid it will answer to and keeps it beside the session.
    vendor_id = str(uuid.uuid4())
    session_dir = paths.session_dir(session_id)
    role_prompt = session_dir / ROLE_PROMPT_FILE
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        text = render_supervisor_prompt(
            front=front, record=record, session_id=session_id, repo=repo,
            branch=branch, role_prompt=role_prompt, predecessor=None)
        write_no_symlink(str(role_prompt), text)
        _write_vendor_session(session_id, vendor_id)
    except Refused as exc:
        return refuse(str(exc))
    except OSError as exc:
        return refuse(f"cannot write launch files: {exc.strerror or exc}")

    if args.dry_run:
        inner = supervisor_inner_command(
            pid_path=paths.session_pid_path(session_id),
            session_id=session_id, repo=repo, role_prompt=role_prompt,
            vendor_id=vendor_id, resume=False)
        argv = supervisor_outer_argv(
            session_id, session_dir / RUN_SCRIPT_FILE, workspace)
        _print_supervisor(session_id, vendor_id, role_prompt, workspace,
                          None, argv, inner)
        print()
        print(text)
        return 0

    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _place_session(
            roster, _supervisor_session(session_id, front, repo,
                                        os.environ.get(caller.SESSION_ENV))),
        default={"sessions": {}},
    )
    argv, inner, pid, failure = _start_supervisor(
        session_id, repo=repo, role_prompt=role_prompt, vendor_id=vendor_id,
        workspace=workspace, resume=False)
    if pid is None:
        store.update_snapshot(
            paths.roster_path(),
            lambda roster: _move_session(roster, session_id, state="failed"),
            default={"sessions": {}},
        )
        return refuse(f"the window launcher failed to start the supervisor: "
                      f"{failure}")
    _record_running(session_id, pid)
    _print_supervisor(session_id, vendor_id, role_prompt, workspace, pid,
                      argv, inner)
    return 0


# --------------------------------------------------------------------------
# `foreman register`: a session Foreman did not start, made visible.
# --------------------------------------------------------------------------


def add_register_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--role", required=True,
                        help="roster role (one of: " + ", ".join(SESSION_ROLES) + ")")
    parser.add_argument("--front", default=None,
                        help="front this session owns; the orchestrator has none")
    parser.add_argument("--pid", required=True, help="pid of the running process")
    parser.add_argument("--session", default=None,
                        help="the vendor's own session id, for a later relaunch")
    parser.add_argument("--pool", default=None, help="pool (default: by role)")
    parser.add_argument("--model", default=None, help="model (default: by pool)")


def _pool_and_model(role: str, pool: str | None,
                    model: str | None) -> tuple[str, str]:
    """What an unstarted session runs on, when the caller does not say.

    The two interactive roles run on Opus; a worker role names its own pool.
    """
    if pool is None:
        pool = SUPERVISOR_POOL if role in (SUPERVISOR, FOREMAN) else role
    if model is None:
        if role in (SUPERVISOR, FOREMAN):
            model = SUPERVISOR_MODEL
        else:
            try:
                model = get_pool(pool).model
            except ValueError:
                model = ""
    return pool, model


@subcommand("register",
            help="Put a session Foreman did not start onto the roster.")
def cmd_register(args: argparse.Namespace) -> int:
    me, violations = caller.resolve("register")
    caller.check_role(me, "register", FOREMAN, SUPERVISOR,
                      violations=violations)
    problems = list(violations)
    if args.role not in SESSION_ROLES:
        problems.append(f"unknown role {args.role!r}; roles the roster knows: "
                        + ", ".join(SESSION_ROLES))
    try:
        pid = int(str(args.pid).strip())
    except (TypeError, ValueError):
        problems.append(f"bad pid {args.pid!r}; a pid is a number")
        pid = None
    starttime = None
    if pid is not None:
        # The start time is read in the same breath as the liveness check
        # and recorded beside the pid: without it the collector cannot tell
        # this process from a later one that reuses its number.
        starttime = procs.proc_starttime(pid)
        if not procs.pid_alive(pid) or starttime is None:
            problems.append(f"pid {pid} is not running; "
                            f"the roster records live processes only")
    if problems:
        return refuse(*problems)
    assert pid is not None

    pool, model = _pool_and_model(args.role, args.pool, args.model)
    session_id = ids.mint("session")
    paths.session_dir(session_id).mkdir(parents=True, exist_ok=True)
    if args.session:
        _write_vendor_session(session_id, str(args.session).strip())
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    session = Session(
        id=session_id,
        role=args.role,
        pool=pool,
        model=model,
        front=args.front,
        pid=pid,
        pgid=pgid,
        pid_starttime=starttime,
        log=str(paths.session_log_path(session_id)),
        launched_by=os.environ.get(caller.SESSION_ENV),
        started_at=store.utcnow_iso(),
        state="running",
    )
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _place_session(roster, session),
        default={"sessions": {}},
    )
    print(f"session: {session_id}")
    print(f"role: {args.role}")
    print(f"front: {args.front or '(none)'}")
    print(f"pid: {pid}")
    return 0


cmd_register.add_arguments = add_register_arguments  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# `foreman relaunch`: the same supervisor, a fresh prompt, its own history.
# --------------------------------------------------------------------------


def add_relaunch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session", help="the session to replace")
    parser.add_argument("--workspace", default=None,
                        help="workspace to open the window on")
    parser.add_argument("--repo", default=None, help="repository to work in")
    parser.add_argument("--branch", default=None, help="the front's branch")
    parser.add_argument("--dry-run", action="store_true",
                        help="render the prompt and print the command; start nothing")


@subcommand("relaunch",
            help="Replace a supervisor with a fresh prompt, resuming its session.")
def cmd_relaunch(args: argparse.Namespace) -> int:
    frozen = paths.frozen_path()
    if frozen.exists():
        return refuse(
            f"frozen file {frozen} exists; the launcher refuses while frozen")
    me, violations = caller.resolve("relaunch")
    caller.check_role(me, "relaunch", FOREMAN, SUPERVISOR,
                      violations=violations)
    problems = list(violations)
    workspace = args.workspace
    if workspace is not None and not WORKSPACE_RE.fullmatch(str(workspace)):
        problems.append(
            f"bad workspace {workspace!r}; a workspace is digits, e.g. 6")

    old_id = args.session
    sessions = caller.read_roster().get("sessions", {})
    old = sessions.get(old_id)
    if not isinstance(old, dict):
        problems.append(f"unknown session '{old_id}'; the roster has no record of it")
        old = {}
    elif (old.get("role") or "") != SUPERVISOR:
        problems.append(
            f"session '{old_id}' has role '{old.get('role') or '(none)'}'; "
            f"only a supervisor is relaunched")
    front = old.get("front")
    record = fronts.read_front_record(front) if front else None
    if old and record is None:
        problems.append(
            f"session '{old_id}' names front '{front or '(none)'}', "
            f"which has no record on the ledger")
    vendor_id = read_vendor_session(old_id) if old else None
    if old and not vendor_id:
        problems.append(
            f"session '{old_id}' has no vendor session id at "
            f"{paths.session_dir(old_id) / VENDOR_SESSION_FILE}; "
            f"there is no conversation to resume")
    repo = os.path.abspath(args.repo or old.get("worktree") or os.getcwd())
    if not os.path.isdir(repo):
        problems.append(f"repo {repo!r} is not a directory")
    if problems:
        return refuse(*problems)
    assert record is not None and vendor_id is not None

    session_id = ids.mint("session")
    branch = args.branch or default_base(repo)
    session_dir = paths.session_dir(session_id)
    role_prompt = session_dir / ROLE_PROMPT_FILE
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        text = render_supervisor_prompt(
            front=front, record=record, session_id=session_id, repo=repo,
            branch=branch, role_prompt=role_prompt, predecessor=old_id)
        write_no_symlink(str(role_prompt), text)
        # The successor answers to the same vendor conversation, so a
        # relaunch of the relaunch resumes it too.
        _write_vendor_session(session_id, vendor_id)
    except Refused as exc:
        return refuse(str(exc))
    except OSError as exc:
        return refuse(f"cannot write launch files: {exc.strerror or exc}")

    if args.dry_run:
        inner = supervisor_inner_command(
            pid_path=paths.session_pid_path(session_id),
            session_id=session_id, repo=repo, role_prompt=role_prompt,
            vendor_id=vendor_id, resume=True)
        argv = supervisor_outer_argv(
            session_id, session_dir / RUN_SCRIPT_FILE, workspace)
        print(f"replaces: {old_id}")
        _print_supervisor(session_id, vendor_id, role_prompt, workspace,
                          None, argv, inner)
        print()
        print(text)
        return 0

    # The predecessor leaves the roster before the successor starts: two
    # live supervisors on one front is the state the collector reads as an
    # intruder, and only one of them holds the front.
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _move_session(roster, old_id, state="exited"),
        default={"sessions": {}},
    )
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _place_session(
            roster, _supervisor_session(session_id, front, repo, old_id)),
        default={"sessions": {}},
    )
    argv, inner, pid, failure = _start_supervisor(
        session_id, repo=repo, role_prompt=role_prompt, vendor_id=vendor_id,
        workspace=workspace, resume=True)
    if pid is None:
        store.update_snapshot(
            paths.roster_path(),
            lambda roster: _move_session(roster, session_id, state="failed"),
            default={"sessions": {}},
        )
        return refuse(f"the window launcher failed to start the supervisor: "
                      f"{failure}")
    _record_running(session_id, pid)
    print(f"replaces: {old_id}")
    _print_supervisor(session_id, vendor_id, role_prompt, workspace, pid,
                      argv, inner)
    return 0


cmd_relaunch.add_arguments = add_relaunch_arguments  # type: ignore[attr-defined]
