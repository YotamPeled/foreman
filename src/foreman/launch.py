"""`foreman launch <role> <pool> <spec>`: summon one worker.

In order: mint a session id; create a git worktree on a new branch from
the base branch; create the per-session directory holding a log file that
is never reused; resolve the timeout from the pool's default unless
``--timeout`` overrides; write ``FOREMAN-JOB.md`` and ``FOREMAN-ROLE.md``
into the worktree; start the process through the pool's adapter; append
the session to the roster snapshot; print the session id, the worktree,
the log path and the pid.

Every path written into the injected files is absolute: a worker process
runs from its own home directory, not from its worktree, so a relative
path there is a dead launch. A relative worktree, spec or log path is
refused before anything is created, and every violated field is named at
once.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from . import ids, paths, store
from .cli import subcommand
from .entities import JOB_KINDS, JOB_ROLES, Session
from .pools import LaunchContext, get as get_pool
from .pools.muse import EFFORTS

JOB_FILE = "FOREMAN-JOB.md"
ROLE_FILE = "FOREMAN-ROLE.md"
PAGE_LINES = 120

VERIFY_HINTS = (
    "verif",
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

TIMEOUT_RE = re.compile(r"(?:\d+[smhd])+$")
UNIT_RANGE_RE = re.compile(r"(\d+)-(\d+)$")


def add_launch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("role", help="worker role (one of: " + ", ".join(JOB_ROLES) + ")")
    parser.add_argument("pool", help="pool to start the worker through")
    parser.add_argument("spec", help="absolute path to the supervisor's spec file")
    parser.add_argument("--component", default=None, help="component name")
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


def read_rulings() -> list[str]:
    texts = []
    for record in store.read_ledger(paths.rulings_path()):
        text = record.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return texts


def format_rulings(texts: list[str]) -> str:
    if not texts:
        return "(no rulings recorded)"
    return "\n".join(f"- {text}" for text in texts)


def lookup_scope(component: str | None, task: str | None) -> str | None:
    if not component or not task:
        return None
    for record in store.read_ledger(paths.component_tasks_path(component)):
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


def build_job_file(job: str, kind: str, task: str, component: str, env: str,
                   rulings: str, scope: str, spec: str,
                   units_label: str | None = None) -> str:
    this_job = f"units: {units_label}\n\n{spec}" if units_label else spec
    return (
        f"# Job {job} \u00b7 {kind} \u00b7 task \"{task}\" \u00b7 component {component}\n"
        "\n"
        "## Environment (injected)\n"
        "\n"
        f"{env}\n"
        "\n"
        "## Rules (injected: swarm + component rulings, verbatim)\n"
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


@subcommand("launch", help="Mint a session, build its worktree, start its worker.")
def cmd_launch(args: argparse.Namespace) -> int:
    problems: list[str] = []

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
        args.worktree or os.path.join(os.path.dirname(repo), f"{os.path.basename(repo.rstrip('/'))}-{session_id}")
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

    session_dir = paths.session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    Path(scratch_dir).mkdir(parents=True, exist_ok=True)
    Path(log_path).touch(exist_ok=False)

    rulings = read_rulings()
    rulings_block = format_rulings(rulings)
    scope = (
        args.scope
        or lookup_scope(args.component, args.task)
        or "(no task scope recorded)"
    )
    env = environment_block(worktree, branch, target, scratch_dir,
                            log_path, verdict_path, timeout)
    job_label = args.job or "(none)"
    task_label = args.task or "(none)"
    component_label = args.component or "(none)"
    job_path = os.path.join(worktree, JOB_FILE)
    role_path = os.path.join(worktree, ROLE_FILE)
    Path(job_path).write_text(
        build_job_file(job_label, args.kind, task_label, component_label,
                       env, rulings_block, scope, spec_text, args.units),
        encoding="utf-8",
    )
    try:
        role_text = render_role_template(args.role, {
            "role": args.role,
            "component": component_label,
            "supervisor": args.component or "(none)",
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
    except Refused as exc:
        return refuse(str(exc))
    Path(role_path).write_text(role_text, encoding="utf-8")

    base_session = Session(
        id=session_id,
        role=args.role,
        pool=args.pool,
        model=adapter.model,
        component=args.component,
        job=args.job,
        pid=None,
        launched_by=os.environ.get("FOREMAN_SESSION"),
        started_at=store.utcnow_iso(),
        state="running",
    )
    ctx = LaunchContext(
        session=base_session,
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
    )

    pid: int | None = None
    if not args.dry_run:
        try:
            pid = adapter.launch(ctx)
        except Exception as exc:  # noqa: BLE001 - a dead spawn is a refusal
            return refuse(f"pool {args.pool} failed to start the process: {exc}")
    command = adapter.command_str(ctx)

    session = Session(
        id=session_id,
        role=args.role,
        pool=args.pool,
        model=adapter.model,
        component=args.component,
        job=args.job,
        pid=pid,
        launched_by=os.environ.get("FOREMAN_SESSION"),
        started_at=base_session.started_at,
        state="running",
    )
    current = store.read_snapshot(paths.roster_path(), default=None) or {}
    sessions = dict(current.get("sessions", {}))
    sessions[session_id] = session.to_dict()
    store.write_snapshot(paths.roster_path(), {"sessions": sessions})

    print(f"session: {session_id}")
    print(f"worktree: {worktree}")
    print(f"log: {log_path}")
    print(f"pid: {pid if pid is not None else '(not started --dry-run)'}")
    print(f"command: {command}")
    return 0


cmd_launch.add_arguments = add_launch_arguments  # type: ignore[attr-defined]
