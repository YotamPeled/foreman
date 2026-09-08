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
pool, and works in the repository on the branch that checkout is already
on — a `--branch` naming any other is refused rather than switched
underneath its owner. Its identity is minted before its process exists —
session id, role prompt and vendor session id are written first, the
window opens second — so it is on the roster from the moment it starts and
is missed by the collector when it goes quiet, and it is recorded running
only once that process is confirmed alive with a start time beside its
pid. One front has one supervisor: a summon for a front that already has a
live one is refused by name.

The prompt it receives is generated, never kept beside the code it
describes: the verbs come from what the CLI registers for the role, the
work from the front's own tasks with their scopes and verification
commands, the rules from the ledger and the brief.

`register` puts a session Foreman never started (a hand-started
supervisor, the orchestrator) on the same roster with the same process
identity; `relaunch` replaces a supervisor with a fresh prompt carrying
its predecessor's last checkpoint, resuming the same vendor conversation.
It stops the predecessor's process before admitting the successor and
records what it stopped, and because a resumed conversation cannot be
handed a new prompt, the fresh one is published at a path the old prompt
already told the session to read.
"""

from __future__ import annotations

import argparse
import ast
import errno
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path

from . import caller, capacity, cli, fronts, hooks, ids, paths, procs, store
from .caller import FOREMAN, MERGE_DESK, SUPERVISOR
from .cli import subcommand
from . import entities
from .entities import JOB_KINDS, JOB_ROLES, SESSION_ROLES, Session
from .pools import LaunchContext, get as get_pool
from .pools import _common
from .pools._common import LAUNCH_EFFORTS as EFFORTS

JOB_FILE = "FOREMAN-JOB.md"
ROLE_FILE = "FOREMAN-ROLE.md"
PAGE_LINES = 120

#: A spec must carry the command its work will be judged by, and this is
#: how that is recognised. It is a heuristic, and it has now refused two
#: honest checks — a `test` comparison on the version zero front and a
#: `python -c` one-liner here — so the shell forms a small check actually
#: takes are listed beside the test runners. A spec whose check is none of
#: these is refused and reworded, which is a cost worth paying: a spec with
#: no verification command in it produces work nobody can judge.
VERIFY_HINTS = (
    "pytest",
    "python -m",
    "python -c",
    "python3 -c",
    "uv run",
    "npm test",
    "npm run",
    "cargo test",
    "go test",
    "make test",
    "make check",
    "foreman-verify",
    "gh pr checks",
    "test ",
    "diff ",
    "grep ",
    "bash -c",
    "sh -c",
)

#: The other shape a check takes: an interpreter run against a file, or an
#: executable run by path. `python app.py` is the most ordinary check a
#: small program has, and the list above accepted `python -m` and
#: `python -c` and refused it — so the version two proof's spec was refused
#: for "no verification command" and reworded to satisfy the checker, which
#: is the defect this whole check exists to prevent on the other side. The
#: file extension (or the leading `./`) is what keeps it from matching
#: prose that merely mentions python.
VERIFY_RUNNER_RE = re.compile(
    r"(?:^|[\s;&|(])"
    r"(?:(?:python3?|node|ruby|perl|bash|sh|zsh|deno|bun)\s+"
    r"[\w./~$-]+\.[A-Za-z0-9]+"
    r"|\./[\w./-]+)"
    r"(?:$|[\s;&|)])")

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
        + "), 'supervisor' for the second shape: launch supervisor <front>, "
        "'merge-desk' for the third: launch merge-desk, "
        "or 'foreman' for the fourth: launch foreman")
    parser.add_argument(
        "pool", nargs="?", default=None,
        help="pool to start the worker through; the front name when "
             "the role is 'supervisor'; nothing when the role is "
             "'merge-desk' or 'foreman'")
    parser.add_argument("spec", nargs="?", default=None,
                        help="absolute path to the supervisor's spec file "
                             "(a supervisor or foreman launch takes none)")
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
    named = any(hint in line for line in lowered for hint in VERIFY_HINTS)
    if not named:
        named = any(VERIFY_RUNNER_RE.search(line) for line in lowered)
    if not named:
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


def checked_out_branch(repo: str) -> str | None:
    """The branch this checkout is on, or None when it is on none."""
    try:
        return run_git(repo, "symbolic-ref", "--short", "HEAD") or None
    except Refused:
        return None


def _branch_of_checkout(repo: str, asked: str | None,
                        problems: list[str]) -> str | None:
    """The branch a supervisor will really work on, or a named refusal.

    A supervisor works in the repository it is given, in a window, beside
    whoever else has that checkout open; switching the branch underneath
    its owner is not a launcher's decision to make. So `--branch` names the
    branch the checkout is already on, or it is refused: naming one branch
    in the prompt and working on another is the failure being closed here.
    """
    if not os.path.isdir(repo):
        return None
    on = checked_out_branch(repo)
    if on is None:
        problems.append(
            f"repo {repo!r} has no branch checked out (detached HEAD, or not "
            f"a git repository); a supervisor works on the branch of the "
            f"checkout it is given, so check one out first")
        return None
    if asked is not None and asked != on:
        problems.append(
            f"branch {asked!r} is not the branch checked out at {repo} "
            f"(that is {on!r}); a supervisor never switches a checkout "
            f"underneath its owner, so check {asked!r} out yourself or drop "
            f"--branch")
        return None
    return on


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


def lookup_task_id(front: str | None, task: str | None) -> str | None:
    """The task id ``--task`` names, whether it was given as id or title.

    ``--task`` takes either, and every reader of a job links it to its task
    by id: a job recorded with a title belongs to no task any screen can
    find, so it is drawn again under the line for jobs whose task nothing
    names. Resolving here is the one place that can fix it, because it is
    the only place that still has the front in hand.
    """
    if not front or not task:
        return None
    for record in store.read_ledger(paths.front_tasks_path(front)):
        if record.get("id") == task or record.get("title") == task:
            found = record.get("id")
            return found if isinstance(found, str) and found else None
    return None


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
    """Assemble ``FOREMAN-JOB.md`` through the ``job`` template.

    The sections and their order are docs/DESIGN.md section 5.2; only
    where the text lives changed, from the f-string below to
    ``templates/job.md``.
    """
    this_job = f"units: {units_label}\n\n{spec}" if units_label else spec
    return render_role_template("job", {
        "job": job,
        "kind": kind,
        "task": task,
        "front": front,
        "role_text": role_text,
        "env": env,
        "rulings": rulings,
        "scope": scope,
        "this_job": this_job,
    })


TEMPLATE_DIR = Path(__file__).with_name("templates")


#: A `{{field}}` in a role template: the whole field contract.
FIELD_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")


def render_role_template(role: str, mapping: dict[str, str]) -> str:
    """Render ``templates/<role>.md`` with ``mapping``, or refuse.

    Both directions are checked at once: a ``{{field}}`` the template
    names and the mapping does not supply would ship to a session
    verbatim, and a mapping key no template names is dead code that
    reads like a promise. A prompt with a hole in it never reaches a
    session.
    """
    path = TEMPLATE_DIR / f"{role}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        known = sorted(p.stem for p in TEMPLATE_DIR.glob("*.md"))
        raise Refused(
            f"unknown role {role!r}; roles with a template: "
            + (", ".join(known) or "(none)")
        ) from None
    named = set(FIELD_RE.findall(text))
    extra = sorted(set(mapping) - named)
    if extra:
        raise Refused(
            f"template {role!r} names no field "
            + ", ".join(f"{{{{{key}}}}}" for key in extra)
            + "; remove the mapping key"
        )
    for key, value in mapping.items():
        text = text.replace("{{" + key + "}}", value)
    holes = sorted(set(FIELD_RE.findall(text)))
    if holes:
        raise Refused(
            f"template {role!r} leaves unsubstituted "
            + ", ".join(f"{{{{{key}}}}}" for key in holes)
            + "; supply every named field"
        )
    return text


def render_worker_prompt(*, role: str, front: str, supervisor: str,
                         session_id: str, verdict_path: str, rulings: str,
                         environment: str) -> str:
    """The worker's role prompt, through the field contract.

    The only mapping a worker template takes: every key here is a
    ``{{field}}`` one of the worker templates names, and every field
    those templates name is here. The worker path in ``launch_main``
    renders through this, never a hand-built mapping.
    """
    return render_role_template(role, {
        "role": role,
        "front": front,
        "supervisor": supervisor,
        "session_id": session_id,
        "verdict_path": verdict_path,
        "rulings": rulings,
        "environment": environment,
    })


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


def remove_dry_run_files(repo: str, branch: str, worktree: str,
                         log_path: str, session_id: str) -> None:
    """Undo everything a dry run wrote. It starts nothing, so it keeps
    nothing.

    A dry run has to build the real thing to print the real command: the
    worktree the worker would work in, the job file and role prompt it
    would read, the log the finish marker would land in. Leaving them
    behind made the launch it was rehearsing impossible — the identical
    real launch is refused for reusing a log and a branch the rehearsal
    took. Found by the panel supervisor reading its own generated prompt.
    """
    remove_launch_worktree(repo, branch, worktree)
    try:
        os.unlink(log_path)
    except OSError:
        pass
    if session_id:
        shutil.rmtree(paths.session_dir(session_id), ignore_errors=True)


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
    # The freeze is a violation like any other, never an early return: a
    # refusal names every violated field at once, so a launch made while
    # frozen and wrong in three other ways is told all four.
    frozen_problems: list[str] = []
    frozen = paths.frozen_path()
    if frozen.exists():
        frozen_problems.append(
            f"frozen file {frozen} exists; the launcher refuses while frozen"
        )
    me, identity_violations = caller.resolve("launch")
    caller.check_role(me, "launch", FOREMAN, SUPERVISOR,
                      violations=identity_violations)
    # The second argument shape, dispatched before the pool is resolved:
    # a supervisor names its front where a worker names its pool. This
    # line is also where the capacity checks stop: they are all below it,
    # on the worker shape only. A supervisor holds no job slot — it is the
    # front's own session, not work inside the front's allocation — so its
    # ceiling is the front existing at all, which `launch supervisor`
    # already requires of the ledger.
    if args.role == SUPERVISOR:
        return launch_supervisor_main(
            args, frozen_problems + list(identity_violations))
    if args.role == MERGE_DESK:
        return launch_merge_desk_main(
            args, frozen_problems + list(identity_violations))
    if args.role == FOREMAN:
        return launch_foreman_main(
            args, frozen_problems + list(identity_violations))
    problems: list[str] = frozen_problems + list(identity_violations)
    if args.spec is None:
        problems.append(
            "spec is required: launch <role> <pool> <spec>, "
            "or launch supervisor <front> for a supervisor")

    adapter = None
    if args.pool is None:
        problems.append(
            "pool is required: launch <role> <pool> <spec>, "
            "or launch supervisor <front> for a supervisor")
    else:
        try:
            adapter = get_pool(args.pool)
        except ValueError as exc:
            problems.append(str(exc))
    if args.role not in JOB_ROLES:
        problems.append(
            f"unknown role {args.role!r}; known roles: " + ", ".join(JOB_ROLES)
        )
    # Capacity, before anything is created: the front's ceiling for this
    # role first, then the pool's cap. Both are read from the open slot
    # grants, so a launch is measured against what is actually held and not
    # against a counter somebody forgot to decrement. A dry run runs this
    # too — the whole point of a dry run is to learn whether the real one
    # would be refused.
    #
    # This read is the refusal the caller sees beside every other violated
    # field, and it is not what admits the launch: `capacity.admit` below
    # checks the same two limits again and takes the slot in the same
    # locked operation, because a check here that a grant honoured later
    # is a race two launchers both win.
    problems.extend(capacity.launch_problems(args.role, args.pool, args.front))
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
    assert spec_text is not None and adapter is not None

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
        role_text = render_worker_prompt(
            role=args.role,
            front=front_label,
            supervisor=args.front or "(none)",
            session_id=session_id,
            verdict_path=verdict_path,
            rulings=rulings_block,
            environment=env,
        )
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
        # The slot is taken here, before the spawn and in one locked
        # operation with the check that allows it. Granting after the spawn
        # left a window in which the ledger said the slot was free and a
        # worker for it was already starting: two launchers in that window
        # both passed a cap of one and both got a process. A launch that
        # names no job takes no job slot (see below), so it is not admitted
        # here either.
        if job_id:
            denied = capacity.admit(
                role=args.role, pool=args.pool, front=args.front,
                job=job_id, session=session_id)
            if denied:
                # A refused launch leaves nothing behind: no slot was
                # taken, the worktree goes, and the record closes.
                store.update_snapshot(
                    paths.roster_path(),
                    lambda roster: _move_session(
                        roster, session_id, state="failed", pid=None,
                        pgid=None),
                    default={"sessions": {}},
                )
                remove_launch_worktree(repo, branch, worktree)
                return refuse(*denied)
        try:
            pid = adapter.launch(ctx)
        except Exception as exc:  # noqa: BLE001 - a dead spawn is a refusal
            # The slot was taken a moment ago for a process that does not
            # exist. Giving it back here is what keeps a pool from filling
            # with grants for workers that never started.
            capacity.release_for_session(
                session_id, store.utcnow_iso(), "failed")
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
        # The slot was taken just above, at the moment the job starts and
        # never at the moment it was planned: a ceiling is not a
        # reservation, so nothing is held while the launch is still only
        # intended. A dry run never reaches here, and a failed spawn gave
        # its slot back, so the ledger holds a grant only for a process
        # that exists. A launch that names no job is not work on a front's
        # queue and takes no job slot: it is a bare worker somebody started
        # by hand, and the pool counts what is dispatched, not what
        # wandered in.
        # In v0 the launcher is the only thing that makes a job exist, so
        # it records the one it just started: without that line the owner
        # sees a session counted in the header and nothing on the screen
        # saying what is running, which is the whole point of the screen.
        if args.front:
            job = entities.Job(
                id=job_id,
                task=lookup_task_id(args.front, args.task) or args.task,
                kind=args.kind, role=args.role,
                spec_path=os.path.abspath(args.spec), session=session_id,
                worktree=worktree, branch=branch, log=log_path,
                timeout=timeout,
                # The units the job was launched to do. They were parsed,
                # validated and written into the job file, and then left
                # off this record, so `job verify` added nothing to its
                # task and no task could ever reach `built` through the
                # runtime. Found by carrying one real job end to end.
                units=units,
                state="running",
                started_at=store.utcnow_iso(),
            )
            store.append_ledger(paths.front_jobs_path(args.front),
                                job.to_dict(), session_id=session_id)
    command = adapter.command_str(ctx)
    if args.dry_run:
        remove_dry_run_files(repo, branch, worktree, log_path, session_id)

    print(f"session: {session_id}")
    print(f"worktree: {worktree}")
    print(f"log: {log_path}")
    print(f"pid: {pid if pid is not None else '(not started --dry-run)'}")
    print(f"command: {command}")
    print_world()
    if not args.dry_run:
        # After the roster write is durable: hooks observe, never gate.
        hooks.fire("on-launch", {
            "session": session_id, "role": args.role, "pool": args.pool,
            "front": args.front, "job": args.job, "worktree": worktree,
            "branch": branch})
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

#: The design's supervisor row (docs/DESIGN.md section 12), verbatim. What
#: this checkout has not shipped is this row minus what the CLI registers
#: for the role, computed at render time: a verb that lands can never be
#: advertised as missing, and a verb that is missing can never be advertised
#: as callable. Nothing else about the verbs is written by hand.
DESIGN_SUPERVISOR_ROW = (
    "checkpoint", "task ready", "task built", "job plan", "job order",
    "job verify", "job fail", "evidence", "finding", "measure", "ask",
    "merge request", "front done",
)

#: The role names the gates are written with, as they read in the source.
_ROLE_CONSTANTS = {"OWNER": caller.OWNER, "FOREMAN": FOREMAN,
                   "SUPERVISOR": SUPERVISOR, "MERGE_DESK": MERGE_DESK}
#: A refusal names the field it refuses on: ``field '--head' is required``.
_FIELD_RE = re.compile(r"field '(-{0,2}[A-Za-z][A-Za-z0-9_-]*)'")


def _role_constant(node: ast.expr) -> str | None:
    """The role a gate argument names, whether bare or qualified.

    ``check_role(me, verb, SUPERVISOR)`` and
    ``check_role(me, verb, caller.SUPERVISOR)`` are the same gate, and a
    reader that saw only the bare name silently gave the verb no roles at
    all — so a supervisor verb written with the qualified spelling was
    listed for nobody. Found when `front take` was missing from a
    supervisor's MCP tools.
    """
    if isinstance(node, ast.Name):
        return _ROLE_CONSTANTS.get(node.id)
    if isinstance(node, ast.Attribute):
        return _ROLE_CONSTANTS.get(node.attr)
    return None


def _called_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _gate_helpers(tree: ast.AST) -> dict[str, set[str]]:
    """Module functions that are a role gate, and the roles each admits.

    A gate helper is a function that calls ``check_role`` with a verb it
    was handed rather than a verb it names — the shape a module reaches for
    when several verbs share one gate. Its callers are the handlers that
    know the verb, so the roles have to travel from here to there.
    """
    helpers: dict[str, set[str]] = {}
    for func in (node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
        names = {arg.arg for arg in func.args.args}
        roles: set[str] = set()
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            called = _called_name(node)
            if called == "check_role" and len(node.args) >= 2:
                if isinstance(node.args[1], ast.Name) and \
                        node.args[1].id in names:
                    roles |= {found for found in
                              (_role_constant(arg) for arg in node.args[2:])
                              if found is not None}
            elif called == "check_front_supervisor" and len(node.args) >= 3:
                if isinstance(node.args[2], ast.Name) and \
                        node.args[2].id in names:
                    roles.add(SUPERVISOR)
        if roles:
            helpers[func.name] = roles
    return helpers


def _module_gates(source: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Read one module's role gates and required fields out of its source.

    The gate is not a table somebody maintains: it is the
    ``check_role(me, "<verb>", ROLE, …)`` call the verb already makes, and
    ``check_front_supervisor`` (through ``_check``), which admits the
    front's own supervisor by definition. The required fields are the
    ``field '<name>' is required`` refusals beside them. Reading the code
    is what keeps the prompt from drifting from it.

    A module may put its gate in a helper — ``_check_desk(me, verb,
    violations)`` — and then the gate call names its verb by a parameter,
    not by a string, so the verb reads as ungated and the tool inventory
    offers it to every role. Helpers are resolved first for that reason: a
    function whose gate names one of its own parameters lends its roles to
    every handler that calls it.

    Returns (verb -> roles allowed, verb -> required field names). A verb
    with no gate at all does not appear here: it is open to every caller.
    """
    gates: dict[str, set[str]] = {}
    required: dict[str, set[str]] = {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return gates, required
    helpers = _gate_helpers(tree)
    for func in (node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
        # `verb = "job verify"` at the top of a handler is the name the
        # gate below it is called with.
        own: str | None = None
        for node in ast.walk(func):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "verb"):
                own = _string(node.value) or own
        local: dict[str, set[str]] = {}
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name == "check_role" and len(node.args) >= 2:
                verb = _string(node.args[1]) or own
                roles = {found for found in
                         (_role_constant(arg) for arg in node.args[2:])
                         if found is not None}
            elif (name in ("check_front_supervisor", "_check")
                    and len(node.args) >= 3):
                verb = _string(node.args[2]) or own
                roles = {SUPERVISOR}
            elif name in helpers and own:
                verb, roles = own, helpers[name]
            else:
                continue
            if verb:
                local.setdefault(verb, set()).update(roles)
        for verb, roles in local.items():
            gates.setdefault(verb, set()).update(roles)
        for node in ast.walk(func):
            text = _string(node)
            if not text or (" is required" not in text
                            and " must be " not in text):
                continue
            match = _FIELD_RE.search(text)
            if not match:
                continue
            # A refusal that names its verb belongs to that verb only; the
            # longest name wins, so 'rule ack' does not read as 'rule'.
            named = [verb for verb in local if verb in text]
            if named:
                targets = [max(named, key=len)]
            else:
                # A refusal naming no verb belongs to the whole function,
                # which is only unambiguous when the function gates one verb.
                targets = list(local) if len(local) == 1 else []
            for verb in targets:
                required.setdefault(verb, set()).add(match.group(1).lstrip("-"))
    return gates, required


def _gate_tables() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Gates and required fields across every module the CLI registers."""
    import_verb_modules()
    gates: dict[str, set[str]] = {}
    required: dict[str, set[str]] = {}
    seen: set[str] = set()
    for handler, _kwargs in list(cli.SUBCOMMANDS.values()):
        name = getattr(handler, "__module__", "")
        module = sys.modules.get(name)
        if module is None or name in seen:
            continue
        seen.add(name)
        try:
            source = inspect.getsource(module)
        except (OSError, TypeError):
            continue
        module_gates, module_required = _module_gates(source)
        for verb, roles in module_gates.items():
            gates.setdefault(verb, set()).update(roles)
        for verb, fields in module_required.items():
            required.setdefault(verb, set()).update(fields)
    return gates, required


def _subparsers(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def import_verb_modules() -> None:
    """Import every module that registers a verb.

    Imported the way the entry point imports them, so a verb is on the
    parser here exactly when `foreman <verb>` runs it. Both readers below
    call this first: the gate reader looks the modules up in
    ``sys.modules``, so a module nothing had imported yet read as having no
    gates at all — and a verb with no gate is a verb open to every role.
    Which modules happened to be imported decided who was offered what.
    """
    from . import collector as _collector  # noqa: F401
    from . import config as _config  # noqa: F401
    from . import doctor as _doctor  # noqa: F401
    from . import fronts as _fronts  # noqa: F401
    from . import hooks as _hooks  # noqa: F401
    from . import mcp as _mcp  # noqa: F401
    from . import measure as _measure  # noqa: F401
    from . import merge as _merge  # noqa: F401
    from . import migrate as _migrate  # noqa: F401
    from . import pool as _pool  # noqa: F401
    from . import progress as _progress  # noqa: F401
    from . import status as _status  # noqa: F401
    from . import verbs as _verbs  # noqa: F401


def registered_verbs() -> list[tuple[str, argparse.ArgumentParser, str]]:
    """Every verb the CLI registers: (path, its parser, its help line)."""
    import_verb_modules()

    found: list[tuple[str, argparse.ArgumentParser, str]] = []

    def walk(parser: argparse.ArgumentParser, prefix: str) -> None:
        action = _subparsers(parser)
        if action is None:
            return
        helps = {choice.dest: (choice.help or "")
                 for choice in action._choices_actions}
        for name, sub in action.choices.items():
            path = f"{prefix} {name}".strip()
            if _subparsers(sub) is None:
                found.append((path, sub, helps.get(name, "")))
            else:
                walk(sub, path)

    walk(cli.build_parser(), "")
    return found


def _positionals(parser: argparse.ArgumentParser) -> list:
    return [action for action in parser._actions
            if not action.option_strings
            and not isinstance(action, argparse._SubParsersAction)]


def _metavar(action) -> str:
    # `--class` is stored as `class_`, the way a keyword has to be; the
    # supervisor types the flag, not the attribute.
    name = (action.metavar or action.dest).rstrip("_")
    return f"<{name} ...>" if action.nargs in ("*", "+") else f"<{name}>"


def _is_required(action, fields: set[str]) -> bool:
    if action.required:
        return True
    names = {action.dest} | {opt.lstrip("-") for opt in action.option_strings}
    return any(field == name or name.startswith(field) or field.startswith(name)
               for field in fields for name in names)


def _verb_line(path: str, parser: argparse.ArgumentParser, help_text: str,
               verb: str, fields: set[str]) -> str:
    """One rendered command: how it is called, and what it is for."""
    extra = len(verb.split()) - len(path.split())
    options = [action for action in parser._actions
               if action.option_strings and action.dest != "help"]
    needed = [action for action in options if _is_required(action, fields)]
    spare = [action for action in options if action not in needed]
    parts = [f"foreman {verb}"]
    if extra > 0:
        # A form the parser does not model (`rule ack` under `rule`): the
        # words are its leading positionals, so what is left is whatever
        # its own refusals say it needs.
        for field in sorted(fields):
            if not any(_is_required(action, {field}) for action in options):
                parts.append(f"<{field}>")
    else:
        for action in _positionals(parser):
            shown = _metavar(action)
            parts.append(f"[{shown}]" if action.nargs == "?" else shown)
    for action in needed:
        flag = action.option_strings[-1]
        parts.append(flag if action.nargs == 0
                     else f"{flag} {_metavar(action)}")
    line = "- `" + " ".join(parts) + "`"
    if help_text:
        line += f" — {help_text}"
    for action in needed:
        if action.help:
            line += f" `{action.option_strings[-1]}`: {action.help}."
    if spare:
        line += (" Also takes: "
                 + ", ".join(f"`{action.option_strings[-1]}`"
                             for action in spare) + ".")
    return line


def supervisor_verbs() -> list[tuple[str, str]]:
    """(verb, rendered line) for every verb a supervisor may actually call.

    Built from the registered parser and the gates in the code behind it,
    never from a hand-written list: a verb this checkout ships for the role
    is in the prompt, and one it does not ship cannot be.
    """
    gates, required = _gate_tables()
    lines: list[tuple[str, str]] = []
    for path, parser, help_text in registered_verbs():
        forms = sorted(verb for verb in gates
                       if verb == path or verb.startswith(path + " "))
        # No gate at all is no refusal at all: the verb is open to everyone.
        allowed = ([verb for verb in forms if SUPERVISOR in gates[verb]]
                   if forms else [path])
        for verb in allowed:
            lines.append((verb, _verb_line(path, parser, help_text, verb,
                                           required.get(verb, set()))))
    return lines


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
                             resume: bool, front_prompt: Path | None = None,
                             mcp_config: Path | None = None) -> str:
    """The shell the window runs: pid first, then the proven vendor line.

    The pid is written as the first act and read back by the launcher, so
    the roster carries the window's own pid rather than the spawner's. The
    session id is exported because every ``foreman`` call the supervisor
    makes takes its caller from the environment; without it the CLI refuses
    its own supervisor as an unregistered writer.

    A resume passes **no positional prompt**: a ``--resume`` with one sits
    idle and never starts. So the fresh prompt is delivered the way a
    resumed conversation can actually take it — the wrapper prints its
    path, and the prompt the session already has tells it to read that
    file first. Writing a file nobody opens is not a relaunch.
    """
    announce = ""
    # The summoned session's verbs arrive as tools, and no other server's
    # do: the config names `foreman mcp` for this session id, and the
    # strict flag ignores every other source. A worker launch passes
    # neither flag on any pool (see the pool adapters), so a worker has
    # no server at all.
    mcp_flags = ""
    if mcp_config is not None:
        mcp_flags = (f" --mcp-config {shlex.quote(str(mcp_config))}"
                     " --strict-mcp-config")
    if resume:
        vendor = (f"{AUTOCOMPACT} claude --resume {shlex.quote(vendor_id)} "
                  f"--model {SUPERVISOR_MODEL} --dangerously-skip-permissions"
                  f"{mcp_flags}")
        where = str(front_prompt if front_prompt is not None else role_prompt)
        announce = "printf '%s\\n' " + shlex.quote(
            "You were relaunched. Read your fresh role prompt before anything "
            "else, as the prompt in this conversation tells you to: "
            + where) + "\n"
    else:
        vendor = (f"{AUTOCOMPACT} claude --session-id {shlex.quote(vendor_id)} "
                  f"--model {SUPERVISOR_MODEL} --dangerously-skip-permissions"
                  f"{mcp_flags} "
                  f'"$(cat {shlex.quote(str(role_prompt))})"')
    return (
        f"echo $$ > {shlex.quote(str(pid_path))}\n"
        f"export {caller.SESSION_ENV}={shlex.quote(session_id)}\n"
        f"cd {shlex.quote(repo)}\n"
        f"{announce}"
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
    """Every task: its line, its scope verbatim, and its verify command.

    A supervisor plans jobs out of the scope and re-runs the verification
    itself, so a prompt carrying titles and states alone carries none of
    the work. Both travel with the task, verbatim from the brief.
    """
    records = store.fold_by_id(
        store.read_ledger(paths.front_tasks_path(front)))
    if not records:
        return "(this front has no tasks on the ledger)"
    blocks = []
    for record in records:
        after = [entry for entry in (record.get("after") or [])
                 if isinstance(entry, str) and entry]
        title = record.get("title") or "(untitled)"
        scope = (record.get("scope") or "").strip()
        verify = (record.get("verify") or "").strip()
        block = [
            f"### Task \"{title}\"",
            "",
            f"- \"{title}\" — {record.get('state') or 'unknown'} · "
            f"size {record.get('size', 0)} · "
            f"units {record.get('units_done', 0)}/"
            f"{record.get('units_total', 0)} · "
            f"after {', '.join(after) if after else 'nothing'}",
            f"- lands on: {record.get('land_on') or '(the front’s branch)'}",
            "- verification, which you re-run yourself before this task is "
            "done: " + (f"`{verify}`" if verify
                        else "(the brief records none: ask before you call "
                             "this task built)"),
            "- scope, verbatim from the brief:",
            "",
            scope or "(the brief records no scope for this task)",
        ]
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def front_monitors_block(record: dict) -> str:
    """The brief's monitors: the question, the command, and how often."""
    monitors = record.get("monitors")
    if not isinstance(monitors, list) or not monitors:
        return "(the brief records no monitor for this front)"
    lines = []
    for monitor in monitors:
        if not isinstance(monitor, dict):
            continue
        alert = monitor.get("alert")
        lines.append(
            f"- \"{monitor.get('question') or '(no question recorded)'}\" — "
            f"measure with `{monitor.get('measure') or '(no command)'}`, "
            f"in {monitor.get('unit') or '(no unit)'} of "
            f"{monitor.get('of', '(no target)')}, every "
            f"{monitor.get('every') or '(no cadence)'}"
            + (f", alert when {alert}" if alert else "")
        )
    return "\n".join(lines) or "(the brief records no monitor for this front)"


def brief_rules(front: str) -> list[str]:
    """The `[[rule]]` lines of the brief, which `front add` stores nowhere.

    The brief is the front's only input and its rules are the owner's; a
    prompt that says "(no rulings recorded)" while the brief carries one
    tells the supervisor the opposite of the truth.
    """
    try:
        with open(paths.brief_path(front), "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    texts = []
    for entry in data.get("rule") or []:
        text = entry.get("text") if isinstance(entry, dict) else None
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts


def supervisor_rulings_block(front: str) -> str:
    """Everything this front runs under, each line saying where it came from."""
    lines = [f"- from the brief: {text}" for text in brief_rules(front)]
    lines += [f"- from the rulings ledger: {text}"
              for text in read_rulings(front)]
    if not lines:
        return ("(no rule is recorded for this front: the swarm's rulings "
                "ledger is empty and the brief carries no `[[rule]]`)")
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
    verbs = supervisor_verbs()
    lines = [line for _verb, line in verbs]
    shipped = {verb for verb, _line in verbs}
    missing = [verb for verb in DESIGN_SUPERVISOR_ROW if verb not in shipped]
    if missing:
        lines.append(
            "- The design gives your role "
            + ", ".join(f"`{verb}`" for verb in missing)
            + " as well; this version does not ship them, so do not call them "
              "and do not invent a substitute.")
    return "\n".join(lines)


def front_prompt_path(front: str) -> Path:
    """Where the front's newest supervisor prompt always is.

    A resumed conversation carries the prompt its predecessor was summoned
    with, not the fresh one, so the fresh one is published at a path the
    old prompt could already name. This is the address a relaunched
    supervisor is told to read, and it is rewritten on every summon.
    """
    return paths.front_dir(front) / "supervisor-prompt.md"


def supervisor_environment_block(*, front: str, repo: str, branch: str,
                                 session_id: str, role_prompt: str,
                                 checkpoint: str) -> str:
    """Every path the supervisor needs, absolute, with none to reconstruct."""
    return (
        f"- repository: {repo} — you work in this checkout\n"
        f"- branch: {branch} — the branch this checkout is on. The launcher "
        f"refuses to summon you onto any other, and you never switch it "
        f"underneath its owner.\n"
        f"- state directory: {paths.state_dir()}\n"
        f"- your session id: {session_id}\n"
        f"- {caller.SESSION_ENV}: must be set to {session_id} on every "
        f"`foreman` call. Your window exports it; a shell you open yourself "
        f"must set it, or the CLI refuses you as an unregistered writer.\n"
        f"- your role prompt: {role_prompt}\n"
        f"- this front's newest role prompt, rewritten on every summon and "
        f"relaunch: {front_prompt_path(front)}\n"
        f"- your checkpoint: {checkpoint}\n"
        f"- the brief, verbatim, the only input this front has: "
        f"{paths.brief_path(front)}\n"
        f"- the plan, when the brief came with one: {paths.plan_path(front)}\n"
        f"- the front ledger: {paths.front_record_path(front)}\n"
        f"- the task ledger: {paths.front_tasks_path(front)}\n"
        f"- the job ledger: {paths.front_jobs_path(front)}\n"
        f"- the evidence ledger: {paths.front_evidence_path(front)}\n"
        f"- the findings ledger: {paths.front_findings_path(front)}\n"
        f"- the measurements ledger: "
        f"{paths.front_measurements_path(front)}\n"
        f"- the rulings ledger: {paths.rulings_path()}\n"
        f"- the roster: {paths.roster_path()}\n"
        f"- the configuration: {paths.config_file()}\n"
        f"- every relative path quoted in the brief, a task scope or a "
        f"monitor is relative to the repository above: `docs/DESIGN.md` is "
        f"{os.path.join(repo, 'docs/DESIGN.md')}"
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
        "monitors": front_monitors_block(record),
        "allocation": allocation_block(record),
        "verbs": verbs_block(),
        "silent_minutes": f"{silent:.0f}",
        "rulings": supervisor_rulings_block(front),
        "predecessor": predecessor_block(predecessor),
        "front_prompt": str(front_prompt_path(front)),
        "environment": supervisor_environment_block(
            front=front, repo=repo, branch=branch, session_id=session_id,
            role_prompt=str(role_prompt),
            checkpoint=str(paths.checkpoint_path(session_id))),
    })


def publish_front_prompt(front: str, text: str) -> Path:
    """Put the fresh prompt where a resumed session was told to look."""
    path = front_prompt_path(front)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_no_symlink(str(path), text)
    return path


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
                      resume: bool,
                      front_prompt: Path | None = None
                      ) -> tuple[list[str], str, int | None, str | None]:
    """Write the window's script, open it, read the pid back.

    Returns the outer argv, the inner shell, the pid and a failure message;
    exactly one of the last two is set. Nothing here raises: this runs after
    the session is on the roster, so a disk or permission error must come
    back as a named refusal that closes the record, never as an exception
    that leaves a session `starting` forever.
    """
    from . import mcp as mcp_module

    session_dir = paths.session_dir(session_id)
    pid_path = paths.session_pid_path(session_id)
    argv: list[str] = []
    inner = ""
    try:
        mcp_config = mcp_module.write_mcp_config(session_id)
    except OSError as exc:
        return argv, inner, None, f"OSError: cannot write MCP config: {exc}"
    inner = supervisor_inner_command(
        pid_path=pid_path, session_id=session_id, repo=repo,
        role_prompt=role_prompt, vendor_id=vendor_id, resume=resume,
        front_prompt=front_prompt, mcp_config=mcp_config)
    argv = supervisor_outer_argv(session_id, session_dir / RUN_SCRIPT_FILE,
                                 workspace)
    try:
        pre_answer_first_launch_dialogs(repo)
        _common.write_worker_script(session_dir / RUN_SCRIPT_FILE, inner)
        pid = _common.spawn_and_wait(
            argv, pid_path=pid_path, session_id=session_id,
            popen=subprocess.Popen)
    except Exception as exc:  # noqa: BLE001 - a dead spawn is a refusal
        return argv, inner, None, f"{type(exc).__name__}: {exc}"
    return argv, inner, pid, None


def live_supervisors(front: str | None = None,
                     ignore: str | None = None) -> list[tuple[str, dict]]:
    """Every live supervisor, or every live supervisor of one front.

    Live means rostered as starting or running *and* the recorded process
    still being that process — a stale record for a pid that is gone, or
    one a later process has reused, holds neither a front nor a slot.

    Both limits over supervisors read this: the front's own (one front, one
    supervisor) filters by front, and the system's ``supervisor_cap`` does
    not, so a machine's supervisors are counted the same way whichever
    refusal is about to be written.
    """
    live: list[tuple[str, dict]] = []
    sessions = caller.read_roster().get("sessions", {})
    for session_id, entry in sessions.items():
        if not isinstance(entry, dict) or session_id == ignore:
            continue
        if entry.get("role") != SUPERVISOR:
            continue
        if front is not None and entry.get("front") != front:
            continue
        if entry.get("state") not in ("starting", "running"):
            continue
        pid = entry.get("pid")
        if pid is None or procs.same_process(pid, entry.get("pid_starttime")):
            live.append((session_id, entry))
    return live


def live_front_supervisor(front: str | None,
                          ignore: str | None = None) -> tuple[str, dict] | None:
    """The front's live supervisor, if it has one.

    One front has one supervisor: two of them is the state the collector
    reads as an intruder, and the one this whole runtime exists to prevent.
    """
    if not front:
        return None
    found = live_supervisors(front, ignore)
    return found[0] if found else None


def print_world() -> None:
    """Name the state directory the summoned session will read.

    Silent when the launcher is on the default world. When it is not, the
    session's world is the launcher's — carried into its script — and
    saying so is the difference between an isolated proof run and a
    session quietly born in the owner's real state directory.
    """
    for name in (paths.STATE_ENV, paths.CONFIG_ENV):
        value = os.environ.get(name)
        if value:
            print(f"{name}: {value} (the session reads this world, not "
                  f"the default one)")


def _print_supervisor(session_id: str, vendor_id: str, role_prompt: Path,
                      workspace: str | None, pid: int | None,
                      argv: list[str], inner: str,
                      front: str | None = None,
                      branch: str | None = None,
                      mcp_config: Path | None = None) -> None:
    print(f"session: {session_id}")
    print(f"vendor session: {vendor_id}")
    print(f"role prompt: {role_prompt}")
    if mcp_config is not None:
        print(f"mcp config: {mcp_config}")
    if front:
        print(f"front prompt: {front_prompt_path(front)}")
    if branch:
        print(f"branch: {branch}")
    where = (f"workspace {workspace}" if workspace is not None
             else "workspace from the window helper")
    print(f"window: {window_name(session_id)} {where}")
    print(f"pid: {pid if pid is not None else '(not started --dry-run)'}")
    print(f"command: {_common.printable_command(argv, inner)}")
    print_world()


def _confirm_started(pid: int | None) -> tuple[int | None, str | None]:
    """The process identity of a launch, or why it is not a launch at all.

    A pid file is not proof that a process lives: the wrapper writes it as
    its first act and can be gone before it is read. A record with no
    process start time beside the pid is worse than none — the collector
    reads a null identity as "trust it" — so a launch that cannot confirm
    both is a refusal and a terminal record, never a running one.
    """
    if pid is None:
        return None, None
    starttime = procs.proc_starttime(pid)
    if not procs.pid_alive(pid) or starttime is None:
        return None, (f"the window wrote pid {pid} and was gone before the "
                      f"launcher could confirm it; a session with no process "
                      f"identity is never recorded running")
    return starttime, None


def _record_running(session_id: str, pid: int, starttime: int) -> None:
    # The starttime beside the pid is the process identity: the collector
    # requires both to match before believing a session alive, so a later
    # process reusing the number cannot inherit this supervisor's slot. It
    # is read before this call and never null here.
    #
    # The group is asked for rather than assumed: unlike a headless worker,
    # whose wrapper runs under setsid and is its own group leader, this
    # shell runs inside a terminal it did not start, so its pid is not its
    # pgid and a kill aimed at the pid as a group would miss.
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


def _record_failed(session_id: str) -> None:
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _move_session(roster, session_id, state="failed",
                                     pid=None, pgid=None),
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
    record = fronts.read_front_record(front) if front is not None else None
    if front is None:
        problems.append(
            "front is required: launch supervisor <front>")
    elif record is None:
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
    branch = _branch_of_checkout(repo, args.branch, problems)
    # One front, one supervisor. A second live one is refused by name here,
    # before anything is minted: two supervisors of one front, both
    # authorised to plan and dispatch, is the failure this runtime exists
    # to prevent, and a replacement goes through `relaunch`.
    live = live_front_supervisor(front) if record is not None else None
    if live is not None:
        held, entry = live
        problems.append(
            f"front '{front}' already has a live supervisor '{held}' "
            f"({entry.get('state')}, pid {entry.get('pid')}); one front has "
            f"one supervisor, so replace it with "
            f"`foreman relaunch {held}` instead of summoning a second")
    # And the system's own limit on supervisors, across every front. The
    # field was loaded and never read, so the cap the owner set on how many
    # fronts may be supervised at once bounded nothing: a summon per front
    # was a summon without a limit. A supervisor is counted apart from the
    # job slots, which is what a separate `supervisor_cap` is for.
    problems.extend(
        capacity.supervisor_problems(front, len(live_supervisors())))
    if problems:
        return refuse(*problems)
    assert record is not None and branch is not None

    session_id = ids.mint("session")
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
        from . import mcp as mcp_module

        try:
            mcp_config = mcp_module.write_mcp_config(session_id)
        except OSError as exc:
            return refuse(f"cannot write launch files: {exc.strerror or exc}")
        inner = supervisor_inner_command(
            pid_path=paths.session_pid_path(session_id),
            session_id=session_id, repo=repo, role_prompt=role_prompt,
            vendor_id=vendor_id, resume=False, mcp_config=mcp_config)
        argv = supervisor_outer_argv(
            session_id, session_dir / RUN_SCRIPT_FILE, workspace)
        _print_supervisor(session_id, vendor_id, role_prompt, workspace,
                          None, argv, inner, front=front, branch=branch,
                          mcp_config=mcp_config)
        print()
        print(text)
        # The session directory stays: a supervisor dry run creates no
        # worktree, no branch and no log, so it blocks no later summon, and
        # reading the generated prompt at its real path is what the dry run
        # is for. The worker launch is the one that had to take its files
        # back (see `remove_dry_run_files`).
        return 0

    try:
        store.update_snapshot(
            paths.roster_path(),
            lambda roster: _place_session(
                roster,
                _supervisor_session(session_id, front, repo,
                                    os.environ.get(caller.SESSION_ENV))),
            default={"sessions": {}},
        )
    except OSError as exc:
        return refuse(f"cannot record the session on the roster: "
                      f"{exc.strerror or exc}")

    # From the roster write to the spawn, every step is inside this handler:
    # past this line the session exists, so a failure has to close it as
    # failed and refuse by name. A session left `starting` forever is a
    # stranded slot nobody can see the end of.
    argv: list[str] = []
    inner = ""
    pid: int | None = None
    failure: str | None = None
    try:
        publish_front_prompt(front, text)
        argv, inner, pid, failure = _start_supervisor(
            session_id, repo=repo, role_prompt=role_prompt,
            vendor_id=vendor_id, workspace=workspace, resume=False)
    except Exception as exc:  # noqa: BLE001 - anything here is a refusal
        failure = f"{type(exc).__name__}: {exc}"
    starttime, dead = _confirm_started(pid)
    if starttime is None:
        _record_failed(session_id)
        return refuse(f"the window launcher failed to start the supervisor: "
                      f"{failure or dead}")
    assert pid is not None
    _record_running(session_id, pid, starttime)
    from . import mcp as mcp_module

    _print_supervisor(session_id, vendor_id, role_prompt, workspace, pid,
                      argv, inner, front=front, branch=branch,
                      mcp_config=mcp_module.mcp_config_path(session_id))
    hooks.fire("on-launch", {"session": session_id, "role": "supervisor",
                             "front": front, "branch": branch})
    return 0


# --------------------------------------------------------------------------
# The third shape: summoning the merge desk.
#
# A desk is summoned the way a supervisor is: an interactive Claude session
# whose identity is minted before its process exists, on the roster from the
# moment it starts. It owns no front, so its prompt carries the merge queue
# instead of a front's tasks, and one desk at a time holds the queue — a
# second summon is refused by name, the way a second supervisor is.
# --------------------------------------------------------------------------

#: The design's merge-desk row (docs/DESIGN.md section 12), verbatim. What
#: this checkout has not shipped is this row minus what the CLI registers
#: for the role, computed at render time like the supervisor's.
DESIGN_DESK_ROW = ("merge take", "merge land", "merge fail")


def desk_verbs() -> list[tuple[str, str]]:
    """(verb, rendered line) for every verb the merge desk may call.

    Built the way :func:`supervisor_verbs` is: from the registered parser
    and the gates in the code behind it, never from a hand-written list.
    """
    gates, required = _gate_tables()
    lines: list[tuple[str, str]] = []
    for path, parser, help_text in registered_verbs():
        forms = sorted(verb for verb in gates
                       if verb == path or verb.startswith(path + " "))
        allowed = ([verb for verb in forms if MERGE_DESK in gates[verb]]
                   if forms else [path])
        for verb in allowed:
            lines.append((verb, _verb_line(path, parser, help_text, verb,
                                           required.get(verb, set()))))
    return lines


def desk_verbs_block() -> str:
    verbs = desk_verbs()
    lines = [line for _verb, line in verbs]
    shipped = {verb for verb, _line in verbs}
    missing = [verb for verb in DESIGN_DESK_ROW if verb not in shipped]
    if missing:
        lines.append(
            "- The design gives your role "
            + ", ".join(f"`{verb}`" for verb in missing)
            + " as well; this version does not ship them, so do not call them "
              "and do not invent a substitute.")
    return "\n".join(lines)


def merge_queue_block() -> str:
    """The merge queue as the desk finds it: waiting oldest first, then
    what already landed or failed. Task ids resolve to titles where a
    front's ledger names them."""
    from . import merge as _merge

    titles: dict[str, str] = {}
    try:
        fronts = sorted(path.name for path in paths.fronts_dir().iterdir()
                        if path.is_dir())
    except OSError:
        fronts = []
    for front in fronts:
        try:
            records = store.read_ledger(paths.front_tasks_path(front))
        except OSError:
            continue
        for record in store.fold_by_id(records):
            if isinstance(record.get("id"), str) and \
                    isinstance(record.get("title"), str):
                titles.setdefault(record["id"], record["title"])
    folded = store.fold_by_id(store.read_ledger(paths.merges_path()))
    if not folded:
        return "(the merge queue is empty)"
    waiting = [row for row in folded
               if not _merge.is_landed(row) and not _merge.is_failed(row)]
    done = [row for row in folded
            if _merge.is_landed(row) or _merge.is_failed(row)]
    waiting.sort(key=lambda row: row.get("requested_at") or "")
    lines = []
    for row in waiting:
        tasks = ", ".join(titles.get(task, task)
                          for task in (row.get("tasks") or []))
        lines.append(
            f"- {row.get('id')}: {row.get('front') or '(no front)'}: "
            f"{row.get('branch')} -> {row.get('target')}, "
            f"lands {tasks or '(no tasks)'} "
            f"(requested {row.get('requested_at') or 'at an unknown time'}"
            + (f", held by {row.get('taken_by')}"
               if row.get("taken_by") else "")
            + ")")
    for row in done:
        if _merge.is_failed(row):
            lines.append(
                f"- {row.get('id')}: {row.get('branch')} -> "
                f"{row.get('target')} failed: "
                f"{row.get('fail_reason') or '(no reason recorded)'}")
        else:
            lines.append(
                f"- {row.get('id')}: {row.get('branch')} -> "
                f"{row.get('target')} landed at {row.get('head') or '?'}")
    return "\n".join(lines)


def desk_environment_block(*, repo: str, branch: str, session_id: str,
                           role_prompt: str) -> str:
    """Every path the desk needs, absolute, with none to reconstruct."""
    return (
        f"- repository: {repo} — you work in this checkout; the branches "
        f"you rebase, check and push live here\n"
        f"- branch: {branch} — the branch this checkout is on. The launcher "
        f"refuses to summon you onto any other, and you never switch it "
        f"underneath its owner except to land a merge.\n"
        f"- state directory: {paths.state_dir()}\n"
        f"- your session id: {session_id}\n"
        f"- {caller.SESSION_ENV}: must be set to {session_id} on every "
        f"`foreman` call. Your window exports it; a shell you open yourself "
        f"must set it, or the CLI refuses you as an unregistered writer.\n"
        f"- your role prompt: {role_prompt}\n"
        f"- the merge ledger: {paths.merges_path()}\n"
        f"- the rulings ledger: {paths.rulings_path()}\n"
        f"- the roster: {paths.roster_path()}\n"
        f"- the configuration (including the target's check command under "
        f"`[merge]`): {paths.config_file()}"
    )


def render_merge_desk_prompt(*, session_id: str, repo: str, branch: str,
                             role_prompt: Path) -> str:
    return render_role_template(MERGE_DESK, {
        "session_id": session_id,
        "queue": merge_queue_block(),
        "verbs": desk_verbs_block(),
        "rulings": format_rulings(read_rulings(None)),
        "environment": desk_environment_block(
            repo=repo, branch=branch, session_id=session_id,
            role_prompt=str(role_prompt)),
    })


def live_merge_desk(ignore: str | None = None
                    ) -> list[tuple[str, dict]]:
    """Every live merge desk: rostered as starting or running *and* still
    that process. One desk at a time holds the queue."""
    live: list[tuple[str, dict]] = []
    sessions = caller.read_roster().get("sessions", {})
    for session_id, entry in sessions.items():
        if not isinstance(entry, dict) or session_id == ignore:
            continue
        if entry.get("role") != MERGE_DESK:
            continue
        if entry.get("state") not in ("starting", "running"):
            continue
        pid = entry.get("pid")
        if pid is None or procs.same_process(pid, entry.get("pid_starttime")):
            live.append((session_id, entry))
    return live


def _merge_desk_session(session_id: str, repo: str,
                        launched_by: str | None) -> Session:
    return Session(
        id=session_id,
        role=MERGE_DESK,
        pool=SUPERVISOR_POOL,
        model=SUPERVISOR_MODEL,
        front=None,
        job=None,
        worktree=repo,
        log=str(paths.session_log_path(session_id)),
        launched_by=launched_by,
        started_at=store.utcnow_iso(),
        state="starting",
    )


def launch_merge_desk_main(args: argparse.Namespace,
                           problems: list[str]) -> int:
    """`foreman launch merge-desk [--workspace N] [--dry-run]`."""
    if args.pool is not None:
        problems.append(
            f"a merge desk takes no pool (got {args.pool!r}): it is the "
            f"desk, not work on one (`foreman launch merge-desk`)")
    if args.spec is not None:
        problems.append(
            "a merge desk takes no spec file: it works the merge queue in "
            "the repository")
    workspace = args.workspace
    if workspace is not None and not WORKSPACE_RE.fullmatch(str(workspace)):
        problems.append(
            f"bad workspace {workspace!r}; a workspace is digits, e.g. 6")
    repo = os.path.abspath(args.repo or os.getcwd())
    if not os.path.isdir(repo):
        problems.append(f"repo {repo!r} is not a directory")
    branch = _branch_of_checkout(repo, args.branch, problems)
    # One desk at a time. A second live one is refused by name here,
    # before anything is minted: two desks, both authorised to land, is
    # the double-landing this runtime exists to prevent.
    live = live_merge_desk()
    if live:
        held, entry = live[0]
        problems.append(
            f"a merge desk is already live as '{held}' "
            f"({entry.get('state')}, pid {entry.get('pid')}); one desk at "
            f"a time holds the queue, so reuse it instead of summoning "
            f"a second")
    if problems:
        return refuse(*problems)
    assert branch is not None

    session_id = ids.mint("session")
    vendor_id = str(uuid.uuid4())
    session_dir = paths.session_dir(session_id)
    role_prompt = session_dir / ROLE_PROMPT_FILE
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        text = render_merge_desk_prompt(
            session_id=session_id, repo=repo, branch=branch,
            role_prompt=role_prompt)
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
                          None, argv, inner, branch=branch)
        print()
        print(text)
        return 0

    try:
        store.update_snapshot(
            paths.roster_path(),
            lambda roster: _place_session(
                roster,
                _merge_desk_session(session_id, repo,
                                    os.environ.get(caller.SESSION_ENV))),
            default={"sessions": {}},
        )
    except OSError as exc:
        return refuse(f"cannot record the session on the roster: "
                      f"{exc.strerror or exc}")

    argv_: list[str] = []
    inner = ""
    pid: int | None = None
    failure: str | None = None
    try:
        argv_, inner, pid, failure = _start_supervisor(
            session_id, repo=repo, role_prompt=role_prompt,
            vendor_id=vendor_id, workspace=workspace, resume=False)
    except Exception as exc:  # noqa: BLE001 - anything here is a refusal
        failure = f"{type(exc).__name__}: {exc}"
    starttime, dead = _confirm_started(pid)
    if starttime is None:
        _record_failed(session_id)
        return refuse(f"the window launcher failed to start the merge desk: "
                      f"{failure or dead}")
    assert pid is not None
    _record_running(session_id, pid, starttime)
    _print_supervisor(session_id, vendor_id, role_prompt, workspace, pid,
                      argv_, inner, branch=branch)
    hooks.fire("on-launch", {"session": session_id, "role": "merge-desk"})
    return 0


# --------------------------------------------------------------------------
# The fourth shape: summoning the foreman.
#
# A foreman is summoned the way a supervisor is: an interactive Claude
# session whose identity is minted before its process exists, on the roster
# from the moment it starts. It owns no front, so its prompt carries the
# fronts instead of one front's tasks, and one foreman at a time holds the
# swarm — a second summon is refused by name, the way a second desk is.
# --------------------------------------------------------------------------

#: The file the foreman's role prompt is written to, beside its session.
FOREMAN_ROLE_FILE = "FOREMAN-ROLE.md"

#: The design's foreman row (docs/DESIGN.md section 12), verbatim. What
#: this checkout has not shipped is this row minus what the CLI registers
#: for the role, computed at render time like the supervisor's.
DESIGN_FOREMAN_ROW = ("admit", "answer", "rule front", "digest", "route")


def foreman_verbs() -> list[tuple[str, str]]:
    """(verb, rendered line) for every verb the foreman may call.

    Built the way :func:`supervisor_verbs` is: from the registered parser
    and the gates in the code behind it, never from a hand-written list.
    """
    gates, required = _gate_tables()
    lines: list[tuple[str, str]] = []
    for path, parser, help_text in registered_verbs():
        forms = sorted(verb for verb in gates
                       if verb == path or verb.startswith(path + " "))
        allowed = ([verb for verb in forms if FOREMAN in gates[verb]]
                   if forms else [path])
        for verb in allowed:
            lines.append((verb, _verb_line(path, parser, help_text, verb,
                                           required.get(verb, set()))))
    return lines


def foreman_verbs_block() -> str:
    verbs = foreman_verbs()
    lines = [line for _verb, line in verbs]
    shipped = {verb for verb, _line in verbs}
    missing = [verb for verb in DESIGN_FOREMAN_ROW if verb not in shipped]
    if missing:
        lines.append(
            "- The design gives your role "
            + ", ".join(f"`{verb}`" for verb in missing)
            + " as well; this version does not ship them, so do not call them "
              "and do not invent a substitute.")
    return "\n".join(lines)


def foreman_fronts_block() -> str:
    """Every front, as the foreman finds it: state, supervisor, landing."""
    try:
        names = sorted(entry.name for entry in paths.fronts_dir().iterdir()
                       if entry.is_dir())
    except OSError:
        names = []
    rows = []
    for name in names:
        record = fronts.read_front_record(name)
        if record is None:
            rows.append(f"- {name} — no record on the ledger")
            continue
        try:
            tasks = store.fold_by_id(
                store.read_ledger(paths.front_tasks_path(name)))
        except OSError:
            tasks = []
        landed = sum(1 for task in tasks
                     if task.get("state") == "landed")
        rows.append(
            f"- {name} — {record.get('state') or 'unknown'} · "
            f"supervisor {record.get('supervisor') or '(none)'} · "
            f"tasks {landed}/{len(tasks)} landed")
    if not rows:
        return ("(no fronts on the ledger: admit nothing until "
                "`front add` puts one there)")
    return "\n".join(rows)


def foreman_environment_block(*, repo: str, branch: str, session_id: str,
                              role_prompt: str) -> str:
    """Every path the foreman needs, absolute, with none to reconstruct."""
    return (
        f"- repository: {repo} — you work in this checkout; the fronts' "
        f"branches live here\n"
        f"- branch: {branch} — the branch this checkout is on. The launcher "
        f"refuses to summon you onto any other, and you never switch it "
        f"underneath its owner.\n"
        f"- state directory: {paths.state_dir()}\n"
        f"- your session id: {session_id}\n"
        f"- {caller.SESSION_ENV}: must be set to {session_id} on every "
        f"`foreman` call. Your window exports it; a shell you open yourself "
        f"must set it, or the CLI refuses you as an unregistered writer.\n"
        f"- your role prompt: {role_prompt}\n"
        f"- the roster: {paths.roster_path()}\n"
        f"- the rulings ledger: {paths.rulings_path()}\n"
        f"- the inbox: {paths.inbox_path()}\n"
        f"- the fronts: {paths.fronts_dir()}\n"
        f"- the configuration: {paths.config_file()}"
    )


def render_foreman_prompt(*, session_id: str, repo: str, branch: str,
                          role_prompt: Path) -> str:
    return render_role_template(FOREMAN, {
        "session_id": session_id,
        "fronts": foreman_fronts_block(),
        "verbs": foreman_verbs_block(),
        "rulings": format_rulings(read_rulings(None)),
        "environment": foreman_environment_block(
            repo=repo, branch=branch, session_id=session_id,
            role_prompt=str(role_prompt)),
    })


def live_foreman(ignore: str | None = None
                 ) -> list[tuple[str, dict]]:
    """Every live foreman: rostered as starting or running *and* still
    that process. One foreman at a time holds the swarm."""
    live: list[tuple[str, dict]] = []
    sessions = caller.read_roster().get("sessions", {})
    for session_id, entry in sessions.items():
        if not isinstance(entry, dict) or session_id == ignore:
            continue
        if entry.get("role") != FOREMAN:
            continue
        if entry.get("state") not in ("starting", "running"):
            continue
        pid = entry.get("pid")
        if pid is None or procs.same_process(pid, entry.get("pid_starttime")):
            live.append((session_id, entry))
    return live


def _foreman_session(session_id: str, repo: str,
                     launched_by: str | None) -> Session:
    return Session(
        id=session_id,
        role=FOREMAN,
        pool=SUPERVISOR_POOL,
        model=SUPERVISOR_MODEL,
        front=None,
        job=None,
        worktree=repo,
        log=str(paths.session_log_path(session_id)),
        launched_by=launched_by,
        started_at=store.utcnow_iso(),
        state="starting",
    )


def launch_foreman_main(args: argparse.Namespace,
                        problems: list[str]) -> int:
    """`foreman launch foreman [--workspace N] [--dry-run]`."""
    if args.pool is not None:
        problems.append(
            f"a foreman takes no pool (got {args.pool!r}): it holds the "
            f"swarm, not work on one (`foreman launch foreman`)")
    if args.spec is not None:
        problems.append(
            "a foreman takes no spec file: it works the front queue in "
            "the repository")
    workspace = args.workspace
    if workspace is not None and not WORKSPACE_RE.fullmatch(str(workspace)):
        problems.append(
            f"bad workspace {workspace!r}; a workspace is digits, e.g. 6")
    repo = os.path.abspath(args.repo or os.getcwd())
    if not os.path.isdir(repo):
        problems.append(f"repo {repo!r} is not a directory")
    branch = _branch_of_checkout(repo, args.branch, problems)
    # One foreman at a time. A second live one is refused by name here,
    # before anything is minted: two foremen, both authorised to admit
    # and route, is the double-swarm this runtime exists to prevent.
    live = live_foreman()
    if live:
        held, entry = live[0]
        problems.append(
            f"a foreman is already live as '{held}' "
            f"({entry.get('state')}, pid {entry.get('pid')}); one foreman at "
            f"a time holds the swarm, so reuse it instead of summoning "
            f"a second")
    if problems:
        return refuse(*problems)
    assert branch is not None

    session_id = ids.mint("session")
    vendor_id = str(uuid.uuid4())
    session_dir = paths.session_dir(session_id)
    role_prompt = session_dir / FOREMAN_ROLE_FILE
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        text = render_foreman_prompt(
            session_id=session_id, repo=repo, branch=branch,
            role_prompt=role_prompt)
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
                          None, argv, inner, branch=branch)
        print()
        print(text)
        # A dry run starts nothing, so it keeps nothing: the session
        # directory it minted goes back, blocking no later summon.
        shutil.rmtree(session_dir, ignore_errors=True)
        return 0

    try:
        store.update_snapshot(
            paths.roster_path(),
            lambda roster: _place_session(
                roster,
                _foreman_session(session_id, repo,
                                 os.environ.get(caller.SESSION_ENV))),
            default={"sessions": {}},
        )
    except OSError as exc:
        return refuse(f"cannot record the session on the roster: "
                      f"{exc.strerror or exc}")

    argv_: list[str] = []
    inner = ""
    pid: int | None = None
    failure: str | None = None
    try:
        argv_, inner, pid, failure = _start_supervisor(
            session_id, repo=repo, role_prompt=role_prompt,
            vendor_id=vendor_id, workspace=workspace, resume=False)
    except Exception as exc:  # noqa: BLE001 - anything here is a refusal
        failure = f"{type(exc).__name__}: {exc}"
    starttime, dead = _confirm_started(pid)
    if starttime is None:
        _record_failed(session_id)
        return refuse(f"the window launcher failed to start the foreman: "
                      f"{failure or dead}")
    assert pid is not None
    _record_running(session_id, pid, starttime)
    _print_supervisor(session_id, vendor_id, role_prompt, workspace, pid,
                      argv_, inner, branch=branch)
    hooks.fire("on-launch", {"session": session_id, "role": "foreman"})
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
        pool = SUPERVISOR_POOL \
            if role in (SUPERVISOR, FOREMAN, MERGE_DESK) else role
    if model is None:
        if role in (SUPERVISOR, FOREMAN, MERGE_DESK):
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
    problems: list[str] = []
    frozen = paths.frozen_path()
    if frozen.exists():
        problems.append(
            f"frozen file {frozen} exists; the launcher refuses while frozen")
    me, violations = caller.resolve("relaunch")
    caller.check_role(me, "relaunch", FOREMAN, SUPERVISOR,
                      violations=violations)
    problems.extend(violations)
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
    branch = _branch_of_checkout(repo, args.branch, problems)
    other = live_front_supervisor(front, ignore=old_id) if old else None
    if other is not None:
        held, entry = other
        problems.append(
            f"front '{front}' already has another live supervisor '{held}' "
            f"({entry.get('state')}, pid {entry.get('pid')}); one front has "
            f"one supervisor, so relaunch that one instead")
    if problems:
        return refuse(*problems)
    assert record is not None and vendor_id is not None and branch is not None

    session_id = ids.mint("session")
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

    old_pid = old.get("pid")
    old_alive = procs.same_process(old_pid, old.get("pid_starttime"))
    if args.dry_run:
        from . import mcp as mcp_module

        try:
            mcp_config = mcp_module.write_mcp_config(session_id)
        except OSError as exc:
            return refuse(f"cannot write launch files: {exc.strerror or exc}")
        inner = supervisor_inner_command(
            pid_path=paths.session_pid_path(session_id),
            session_id=session_id, repo=repo, role_prompt=role_prompt,
            vendor_id=vendor_id, resume=True,
            front_prompt=front_prompt_path(front), mcp_config=mcp_config)
        argv = supervisor_outer_argv(
            session_id, session_dir / RUN_SCRIPT_FILE, workspace)
        print(f"replaces: {old_id}")
        print(f"would stop: pid {old_pid}" if old_alive
              else f"would stop: nothing ({old_id} is not running)")
        _print_supervisor(session_id, vendor_id, role_prompt, workspace,
                          None, argv, inner, front=front, branch=branch,
                          mcp_config=mcp_config)
        print()
        print(text)
        return 0

    # The predecessor is stopped before the successor is admitted, and the
    # roster says what was stopped. Marking a record exited while its
    # process runs on, still able to plan, dispatch and checkpoint, leaves
    # two live supervisors of one front: the failure this runtime exists to
    # prevent. Authority follows the process, so the process ends first.
    stopped: list[int] = []
    if old_alive:
        signalled, remaining = procs.kill_job(old_pid, old.get("pgid"))
        if remaining:
            return refuse(
                f"session '{old_id}' is still alive after being stopped "
                f"(pids {sorted(remaining)}); no successor was started, "
                f"because two live supervisors of one front is the failure "
                f"this runtime exists to prevent")
        stopped = signalled
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _move_session(
            roster, old_id, state="exited", pid=None, pgid=None,
            stopped_pid=old_pid if old_alive else None,
            stopped_pids=stopped,
            stopped_at=store.utcnow_iso() if old_alive else None,
            stopped_by=session_id),
        default={"sessions": {}},
    )
    try:
        store.update_snapshot(
            paths.roster_path(),
            lambda roster: _place_session(
                roster, _supervisor_session(session_id, front, repo, old_id)),
            default={"sessions": {}},
        )
    except OSError as exc:
        return refuse(f"cannot record the session on the roster: "
                      f"{exc.strerror or exc}")

    argv: list[str] = []
    inner = ""
    pid: int | None = None
    failure: str | None = None
    try:
        publish_front_prompt(front, text)
        argv, inner, pid, failure = _start_supervisor(
            session_id, repo=repo, role_prompt=role_prompt,
            vendor_id=vendor_id, workspace=workspace, resume=True,
            front_prompt=front_prompt_path(front))
    except Exception as exc:  # noqa: BLE001 - anything here is a refusal
        failure = f"{type(exc).__name__}: {exc}"
    starttime, dead = _confirm_started(pid)
    if starttime is None:
        _record_failed(session_id)
        return refuse(f"the window launcher failed to start the supervisor: "
                      f"{failure or dead}")
    assert pid is not None
    _record_running(session_id, pid, starttime)
    print(f"replaces: {old_id}")
    print(f"stopped: pid {old_pid} ({len(stopped)} process(es))" if stopped
          else f"stopped: nothing ({old_id} was not running)")
    from . import mcp as mcp_module

    _print_supervisor(session_id, vendor_id, role_prompt, workspace, pid,
                      argv, inner, front=front, branch=branch,
                      mcp_config=mcp_module.mcp_config_path(session_id))
    hooks.fire("on-launch", {"session": session_id, "role": "supervisor",
                             "front": front, "branch": branch,
                             "replaces": old_id})
    return 0


cmd_relaunch.add_arguments = add_relaunch_arguments  # type: ignore[attr-defined]
