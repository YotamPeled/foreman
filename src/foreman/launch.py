"""`foreman launch <role> <pool> <spec>`: summon one worker.

In order: refuse while the frozen file exists and refuse callers outside
the foreman and supervisor roles; mint a session id; create a git worktree
on a new branch from the base branch (a review of an existing branch
checks that branch out detached instead, and creates no branch); create
the per-session directory holding a log file that is never reused;
resolve the timeout from the pool's default unless ``--timeout``
overrides; write ``FOREMAN-JOB.md`` (with the role prompt embedded, so
the worker actually receives it) and ``FOREMAN-ROLE.md`` into the
worktree, refusing symlinks; record the session on the roster snapshot
under one lock; start the process through the pool's adapter and move
the record to running with its pid and process group; print the session
id, the worktree, the log path and the pid. A failure before the spawn
removes the worktree and, when the launch created the branch, the
branch; a failed spawn is recorded as failed, never running; a dry run
records nothing.

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
identity. `relaunch` never resumes: it stops the old process and summons a
*fresh* vendor conversation under the same Foreman session id, with a
newly rendered role prompt as its positional argument, carrying the
front's latest checkpoint so the supervisor picks up where it left off.
The new vendor id is recorded on the roster; a resumed conversation could
not be handed a prompt at all, and the one this replaces sat idle waiting
for a person at the keyboard.

`kill` is the other half: it stops the transient unit the launcher
started, then the recorded pid, and writes the roster line — and the job
line, when the id names a job — killed with who killed it and why,
releasing the slot. Only the recorded unit and pid are ever signalled;
nothing here matches a process by pattern.
"""

from __future__ import annotations

import argparse
import ast
import difflib
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
    parser.add_argument("--on", "--task", dest="task", default=None,
                        help="task id or title (--task or --on)")
    parser.add_argument("--job", default=None, help="job id this session runs")
    parser.add_argument("--kind", default="implement", choices=JOB_KINDS)
    parser.add_argument("--branch", default=None, help="new branch for the worktree")
    parser.add_argument("--base", default=None, help="base branch (merge target)")
    parser.add_argument("--repo", default=None, help="repo to create the worktree from")
    parser.add_argument("--worktree", default=None, help="absolute worktree path")
    parser.add_argument("--log", default=None, help="absolute log path")
    parser.add_argument("--timeout", default=None, help="e.g. 20m (pool default otherwise)")
    parser.add_argument("--units", default=None,
                        help="unit count, e.g. 4 (default: every unit "
                             "the task still lacks)")
    parser.add_argument("--effort", default="high", choices=EFFORTS)
    parser.add_argument("--scope", default=None, help="task scope text")
    parser.add_argument("--window", action="store_true",
                        help="owner debugging only: open this worker in a "
                             "terminal window. Requires "
                             + WORKER_WINDOW_ENV + "=1 and refuses any caller "
                             "but the owner; workers and reviewers are never "
                             "on screen.")
    parser.add_argument("--headless", action="store_true",
                        help="summon a supervisor or the merge desk with no "
                             "window: the session runs one `claude -p` turn "
                             "per wake and holds no long-lived process. "
                             "With neither this nor --no-headless the "
                             "[launch] headless flag decides.")
    parser.add_argument("--no-headless", action="store_true",
                        help="summon with a window even when [launch] "
                             "headless is true.")
    parser.add_argument("--model", default=None,
                        help="model a supervisor, merge-desk or foreman "
                             "launch runs (default: claude-opus-5); "
                             "refused for a worker, whose model is its pool's")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except start the process; print the command")


class Refused(Exception):
    """A launch refusal: str lists every violated field at once."""


def parse_units(text: str) -> int:
    """A unit count, never unit ids: ``--units 4`` is four units.

    A range or list of ids (``3-7``, ``1,2``) is refused: it named which
    units, not how many, so a supervisor asking for four units credited
    one and `task built` refused work that was delivered. Zero is a real
    count: a repair job on a task that already earned its units launches
    with `--units 0`.
    """
    piece = (text or "").strip()
    if piece.isdigit():
        return int(piece)
    raise ValueError(
        f"bad units {text!r}; --units takes a count, e.g. --units 4")


def default_unit_count(front: str | None, task: str | None) -> int:
    """Every unit the task still lacks: total minus done, never negative.

    No task (a bare worker) or a task the ledger does not name lacks
    nothing countable, so the default is zero there rather than a guess.
    """
    task_id = lookup_task_id(front, task)
    if not front or not task_id:
        return 0
    try:
        folded = store.fold_by_id(
            store.read_ledger(paths.front_tasks_path(front)))
    except OSError:
        return 0
    for record in folded:
        if record.get("id") == task_id:
            try:
                total = int(record.get("units_total") or 0)
                done = int(record.get("units_done") or 0)
            except (TypeError, ValueError):
                return 0
            return max(0, total - done)
    return 0


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


def local_branch_exists(repo: str, name: str) -> bool:
    """True when ``refs/heads/<name>`` is a local branch of ``repo``."""
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet",
         f"refs/heads/{name}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return proc.returncode == 0


def default_base(repo: str) -> str:
    try:
        return run_git(repo, "symbolic-ref", "--short", "HEAD")
    except Refused:
        return "HEAD"


def has_origin_remote(repo: str) -> bool:
    """True when ``repo`` lists a remote named ``origin``."""
    proc = subprocess.run(
        ["git", "-C", repo, "remote"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return proc.returncode == 0 and "origin" in proc.stdout.splitlines()


def _short_sha(sha: str) -> str:
    return sha[:7]


def _local_vs_origin(repo: str, local_sha: str, origin_sha: str) -> str | None:
    """``behind``, ``ahead of`` or ``diverged from``, or None when equal."""
    if local_sha == origin_sha:
        return None
    behind = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor",
         local_sha, origin_sha],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if behind.returncode == 0:
        return "behind"
    ahead = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor",
         origin_sha, local_sha],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if ahead.returncode == 0:
        return "ahead of"
    return "diverged from"


def resolve_base(repo: str, branch: str) -> tuple[str, str, str | None]:
    """Resolve ``branch`` against origin when the repository has one.

    Returns ``(start_ref, full_sha, printed_line)``. ``start_ref`` is
    ``origin/<branch>`` after a successful fetch, or ``<branch>`` when
    there is no origin. ``printed_line`` is the comparison (or the no
    remote line), or None when there is nothing to say. Fetch failure
    raises :class:`Refused` naming the branch and git's stderr. A
    repository with no origin and no local branch returns
    ``(<branch>, "", None)`` so the caller can refuse in its own words
    or let ``git worktree add`` fail as it does today.
    """
    if not has_origin_remote(repo):
        if not local_branch_exists(repo, branch):
            return branch, "", None
        sha = run_git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
        return (
            branch,
            sha,
            f"base {branch}: no remote; local {_short_sha(sha)}",
        )
    fetched = subprocess.run(
        ["git", "-C", repo, "fetch", "origin", branch],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if fetched.returncode != 0:
        raise Refused(
            f"cannot fetch origin {branch!r}: {fetched.stderr.strip()}"
        )
    start = f"origin/{branch}"
    origin_sha = run_git(repo, "rev-parse", "--verify", start)
    note = None
    if local_branch_exists(repo, branch):
        local_sha = run_git(
            repo, "rev-parse", "--verify", f"refs/heads/{branch}")
        relation = _local_vs_origin(repo, local_sha, origin_sha)
        if relation is not None:
            note = (
                f"base {branch}: local {_short_sha(local_sha)} is "
                f"{relation} origin {_short_sha(origin_sha)}; using origin"
            )
    return start, origin_sha, note


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


#: How many dirty paths a refusal prints before it says "and N more":
#: enough to recognise the work, short enough to stay one screen.
DIRTY_SHOWN = 10


def uncommitted_paths(repo: str) -> list[str]:
    """Every path git reports as changed or untracked, or an empty list.

    A checkout that cannot be read is not evidence of a clean one, but it
    is not evidence of a dirty one either: the branch check above already
    refuses a directory that is not a git repository, so silence here
    means nothing to report.
    """
    try:
        done = subprocess.run(
            ["git", "-C", repo, "status", "--porcelain"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [line[3:].strip() for line in done.stdout.splitlines()
            if line.strip()]


def dirty_tree_problem(repo: str) -> str | None:
    """The refusal a relaunch owes a working tree nobody accounted for.

    A session summoned into a checkout carrying uncommitted work inherits
    changes it did not make and cannot explain: it will either commit
    somebody else's work as its own or throw it away. Neither is the
    launcher's call, so the relaunch refuses and names the files, and the
    supervisor rules keep or discard before asking again. Seen on the
    panel front: a dead job's worktree left two modified files on top of
    its last commit.
    """
    paths_dirty = uncommitted_paths(repo)
    if not paths_dirty:
        return None
    shown = paths_dirty[:DIRTY_SHOWN]
    more = len(paths_dirty) - len(shown)
    listed = ", ".join(shown) + (f", and {more} more" if more else "")
    return (f"checkout {repo} has uncommitted changes ({listed}); a "
            f"relaunched session must not inherit work nobody accounted "
            f"for, so keep it (commit it) or discard it (git restore / git "
            f"clean) and relaunch again")


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


def unknown_task_message(front: str, task: str) -> str:
    """The one unknown-task refusal, with the nearest title.

    Launch collects this with every other violated field. ``nearest`` is
    the closest current title on the front, or ``(none)`` when the front
    has no titles at all.
    """
    titles: list[str] = []
    for record in store.fold_by_id(
            store.read_ledger(paths.front_tasks_path(front))):
        title = record.get("title")
        if isinstance(title, str) and title:
            titles.append(title)
    matches = difflib.get_close_matches(task, titles, n=1, cutoff=0.0)
    nearest = f"{matches[0]!r}" if matches else "(none)"
    return (f"--task {task!r} names no task on front {front!r}; "
            f"nearest: {nearest}")


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
        "(a finish line naming the worker's exit code is appended to it "
        "when the worker exits; `foreman wait <session>` is how a launcher "
        "learns the outcome)\n"
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


def worker_template(role: str, kind: str | None) -> str:
    """The template name for one worker launch: role, then job kind.

    A role's template used to be its pool's name alone, which made the
    prompt a property of who was cheap rather than of what the job was:
    the same file was served whether the launcher asked for an
    implementation or a review. A pool that does both names one template
    per kind (``grok.implement.md``), and the bare ``grok.md`` answers
    for every kind it does not name, so a pool that only ever does one
    thing keeps the one file it has.
    """
    want = (kind or "").strip()
    if want and (TEMPLATE_DIR / f"{role}.{want}.md").is_file():
        return f"{role}.{want}"
    return role


def render_worker_prompt(*, role: str, front: str, supervisor: str,
                         session_id: str, verdict_path: str, rulings: str,
                         environment: str, kind: str | None = None) -> str:
    """The worker's role prompt, through the field contract.

    The only mapping a worker template takes: every key here is a
    ``{{field}}`` one of the worker templates names, and every field
    those templates name is here. The worker path in ``launch_main``
    renders through this, never a hand-built mapping.
    """
    return render_role_template(worker_template(role, kind), {
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


def remove_launch_worktree(repo: str, branch: str, worktree: str,
                           *, delete_branch: bool = True) -> None:
    """Best-effort undo of the worktree half of a failed launch.

    A review of an existing branch did not create that branch, so
    ``delete_branch`` is false and the branch stays exactly where it was.
    """
    try:
        run_git(repo, "worktree", "remove", "--force", worktree)
    except Refused:
        pass
    if delete_branch:
        try:
            run_git(repo, "branch", "-D", branch)
        except Refused:
            pass


def remove_dry_run_files(repo: str, branch: str, worktree: str,
                         log_path: str, session_id: str,
                         *, delete_branch: bool = True) -> None:
    """Undo everything a dry run wrote. It starts nothing, so it keeps
    nothing.

    A dry run has to build the real thing to print the real command: the
    worktree the worker would work in, the job file and role prompt it
    would read, the log the finish marker would land in. Leaving them
    behind made the launch it was rehearsing impossible — the identical
    real launch is refused for reusing a log and a branch the rehearsal
    took. Found by the panel supervisor reading its own generated prompt.
    A review of an existing branch leaves that branch; only the worktree
    the rehearsal created goes.
    """
    remove_launch_worktree(repo, branch, worktree,
                           delete_branch=delete_branch)
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


def _adapter_model(adapter, role: str) -> str:
    """The model recorded on a worker session.

    An adapter that pins a model per role answers that pin (the packaged
    claude pool, so an opus worker still records ``claude-opus-5``). Every
    other adapter answers its ``model`` attribute, which for a directory
    pool is the manifest's — so a user pool wrapping claude launches the
    model it named, not the role pin.
    """
    method = getattr(adapter, "model_for_role", None)
    if callable(method):
        named = method(role)
        if isinstance(named, str) and named:
            return named
    return adapter.model


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
    if getattr(args, "model", None):
        problems.append(
            f"--model {args.model!r} is refused: a worker's model is "
            f"its pool's")
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
        explicit_units = parse_units(args.units) \
            if args.units is not None else None
    except ValueError as exc:
        problems.append(str(exc))
        explicit_units = None

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

    # --task names a task that exists on a front. A title no task carries
    # used to launch anyway, and only job verify refused afterwards.
    if args.task and not args.front:
        problems.append(
            "--task is refused without --front: a task belongs to a front")
    elif args.front and args.task and lookup_task_id(
            args.front, args.task) is None:
        problems.append(unknown_task_message(args.front, args.task))

    # --base against origin, when given. default_base is unchanged when
    # the flag is absent. Fetch failure joins this list so it is named
    # with every other refusal at once.
    cut_from: str | None = None
    base_sha = ""
    base_note: str | None = None
    if args.base is not None and os.path.isdir(repo):
        try:
            cut_from, base_sha, base_note = resolve_base(repo, args.base)
        except Refused as exc:
            problems.append(str(exc))

    # A review of --branch resolves the same way: fetch, prefer
    # origin/<branch>, say which. A missing local branch with no origin
    # keeps the existing refusal, collected here with the rest.
    review_start: str | None = None
    review_note: str | None = None
    if args.kind == "review" and args.branch and os.path.isdir(repo):
        try:
            start, sha, note = resolve_base(repo, args.branch)
        except Refused as exc:
            problems.append(str(exc))
        else:
            if sha:
                review_start = start
                review_note = note
            else:
                problems.append(
                    f"branch {args.branch!r} does not exist in {repo}; "
                    f"a review reads an existing branch")

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

    # The count travels, not the flag: an explicit `--units n` credits n,
    # and the default is every unit the task still lacks.
    units = explicit_units if explicit_units is not None \
        else default_unit_count(args.front, args.task)

    session_id = ids.mint("session")
    branch = args.branch or f"foreman/{session_id}"
    target = args.base or default_base(repo)
    start_point = cut_from or target
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

    # A review of an existing branch reads that branch: detached at its
    # head, no new branch. Naming a branch that is not there is a
    # refusal, not a throwaway cut from --base. Every other kind still
    # cuts a new branch from the target.
    created_branch = True
    try:
        if args.kind == "review" and args.branch:
            run_git(repo, "worktree", "add", "--detach", worktree,
                    review_start or args.branch)
            created_branch = False
        else:
            run_git(repo, "worktree", "add", "-b", branch, worktree,
                    start_point)
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
            kind=args.kind,
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
                           env, rulings_block, scope, spec_text, str(units),
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
            model=_adapter_model(adapter, args.role),
            front=args.front,
            job=job_id,
            pid=None,
            pgid=None,
            worktree=worktree,
            branch=branch,
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
            units=units,
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
        remove_launch_worktree(repo, branch, worktree,
                               delete_branch=created_branch)
        return refuse(str(exc))
    except OSError as exc:
        remove_launch_worktree(repo, branch, worktree,
                               delete_branch=created_branch)
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
                remove_launch_worktree(repo, branch, worktree,
                                       delete_branch=created_branch)
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
                worktree=worktree, branch=branch, base=target,
                base_sha=base_sha,
                log=log_path, timeout=timeout,
                # The unit count the job was launched to do. It was parsed,
                # validated and written into the job file, and then left
                # off this record, so `job verify` added nothing to its
                # task and no task could ever reach `built` through the
                # runtime. Found by carrying one real job end to end.
                # `base` is what a later branch-moved check measures the
                # branch against: without it a failed job's work is
                # uncountable.
                units=units,
                state="running",
                started_at=store.utcnow_iso(),
            )
            store.append_ledger(paths.front_jobs_path(args.front),
                                job.to_dict(), session_id=session_id)
    command = adapter.command_str(ctx)
    if args.dry_run:
        remove_dry_run_files(repo, branch, worktree, log_path, session_id,
                             delete_branch=created_branch)

    if base_note:
        print(base_note)
    if review_note and review_note != base_note:
        print(review_note)
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


def _resolve_summoned_model(args: argparse.Namespace) -> str:
    """The model a supervisor, desk or foreman launch runs.

    ``--model`` names it; without the flag the packaged supervisor model
    is the default. A worker launch never reaches here: its model is
    its pool's.
    """
    value = getattr(args, "model", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return SUPERVISOR_MODEL
#: The vendor's own session id (a uuid) lives beside the session as well as
#: on its roster record: Foreman mints the roster id and the vendor answers
#: to this one. A relaunch mints a fresh vendor id under the same Foreman
#: session id and rewrites both, so what the roster names is the
#: conversation that is actually running.
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


def _packaged_defaults() -> dict:
    """Foreman's own shipped configuration, parsed."""
    from .config import DEFAULT_TEXT

    return tomllib.loads(DEFAULT_TEXT)


def _user_config() -> dict:
    with open(paths.config_file(), "rb") as handle:
        return tomllib.load(handle)


def _configured_default_workspace() -> tuple[str | None, str | None]:
    """The configured default window workspace: (valid, malformed).

    Exactly one of the two is set. ``default_workspace`` is read from the
    ``[launch]`` table first and the top level second, as an integer or a
    string of digits — the same shape ``--workspace`` takes.

    The packaged defaults answer where the user's file is silent, the way
    every other configured value is layered: a fresh install ships
    ``default_workspace``, so a machine nobody has configured still has
    somewhere to put a window. A file that does not parse means no
    default, never a crash: the caller refuses the launch by name.
    """
    candidates: list[object] = []
    # The user's file first, both spellings, then the packaged defaults:
    # a value the owner wrote always beats the one Foreman ships, at
    # either level.
    for source in (_user_config, _packaged_defaults):
        try:
            data = source()
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        table = data.get("launch")
        if isinstance(table, dict) and \
                table.get("default_workspace") is not None:
            candidates.append(table.get("default_workspace"))
        if data.get("default_workspace") is not None:
            candidates.append(data.get("default_workspace"))
        if candidates:
            break
    for value in candidates:
        if isinstance(value, bool):
            text = ""
        elif isinstance(value, int):
            text = str(value)
        elif isinstance(value, str):
            text = value.strip()
        else:
            text = ""
        if text and WORKSPACE_RE.fullmatch(text):
            return text, None
        if text or value is not None:
            return None, str(value)
    return None, None


def configured_default_workspace() -> str | None:
    """The configured ``[launch] default_workspace``, or None.

    Doctor's reader: it reports the absence, so it needs the value alone
    without the launcher's refusal machinery around it.
    """
    default, _bad = _configured_default_workspace()
    return default


def _resolve_window_workspace(given: str | None,
                              problems: list[str]) -> str | None:
    """The workspace a windowed launch opens on, or a named refusal.

    An explicit ``--workspace`` wins; without one the launch lands on
    ``default_workspace`` from the configuration. With neither, the launch
    is refused here — while it is still a list of problems, before a
    session is minted or a byte is written — so it never spends thirty
    seconds waiting on a pid file no window will write.
    """
    if given is not None:
        if not WORKSPACE_RE.fullmatch(str(given)):
            problems.append(
                f"bad workspace {given!r}; a workspace is digits, e.g. 6")
            return None
        return str(given)
    default, bad = _configured_default_workspace()
    if default is not None:
        return default
    if bad is not None:
        problems.append(
            f"bad default_workspace {bad!r} in {paths.config_file()}; "
            f"a workspace is digits, e.g. 6")
        return None
    problems.append(
        f"field '--workspace' is required for a windowed launch "
        f"(no default_workspace in {paths.config_file()}); "
        f"pass --workspace <n>")
    return None

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
    from . import attach as _attach  # noqa: F401
    from . import collector as _collector  # noqa: F401
    from . import config as _config  # noqa: F401
    from . import doctor as _doctor  # noqa: F401
    from . import fronts as _fronts  # noqa: F401
    from . import headless as _headless  # noqa: F401
    from . import hooks as _hooks  # noqa: F401
    from . import mcp as _mcp  # noqa: F401
    from . import measure as _measure  # noqa: F401
    from . import merge as _merge  # noqa: F401
    from . import migrate as _migrate  # noqa: F401
    from . import pool as _pool  # noqa: F401
    from . import progress as _progress  # noqa: F401
    from . import status as _status  # noqa: F401
    from . import verbs as _verbs  # noqa: F401
    from . import wait as _wait  # noqa: F401
    from . import wake as _wake  # noqa: F401


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

    def matches(name: str, field: str) -> bool:
        if name == field:
            return True
        if name.startswith(field):
            rest = name[len(field):]
        elif field.startswith(name):
            rest = field[len(name):]
        else:
            return False
        # dest `class_` matches field `class`; `--output-file` (and dest
        # `output_file`) must not match field `output`.
        return rest == "_" or not rest.startswith(("-", "_"))

    return any(matches(name, field) for field in fields for name in names)


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
                             mcp_config: Path | None = None,
                             model: str | None = None) -> str:
    """The shell the window runs: pid first, then the proven vendor line.

    The pid is written as the first act and read back by the launcher, so
    the roster carries the window's own pid rather than the spawner's. The
    session id is exported because every ``foreman`` call the supervisor
    makes takes its caller from the environment; without it the CLI refuses
    its own supervisor as an unregistered writer.

    There is one shape here and no second one: a fresh vendor session with
    the role prompt as its positional argument. A relaunch used to pass
    ``--resume`` with no prompt, because a resumed conversation cannot be
    handed one, and printed the fresh prompt's path to the terminal
    instead. The terminal is not the session: proven on the skills front,
    where the resumed process wrote nothing for twenty-six minutes while
    its front waited on one verb. So a relaunch summons a new conversation
    with the prompt in it, and continuity comes from the prompt carrying
    the front's latest checkpoint.
    """
    # The summoned session's verbs arrive as tools, and no other server's
    # do: the config names this checkout's interpreter running
    # `foreman mcp` for this session id (never a bare `foreman` off PATH,
    # so a branch checkout's session runs that branch's verbs), and the
    # strict flag ignores every other source. A worker launch passes
    # neither flag on any pool (see the pool adapters), so a worker has
    # no server at all.
    mcp_flags = ""
    if mcp_config is not None:
        mcp_flags = (f" --mcp-config {shlex.quote(str(mcp_config))}"
                     " --strict-mcp-config")
    vendor = (f"{AUTOCOMPACT} claude --session-id {shlex.quote(vendor_id)} "
              f"--model {shlex.quote(model or SUPERVISOR_MODEL)} "
              f"--dangerously-skip-permissions"
              f"{mcp_flags} "
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
        added = (f" · added by {record.get('added_by')}"
                 if record.get("added_by") else "")
        block = [
            f"### Task \"{title}\"",
            "",
            f"- \"{title}\" — {record.get('state') or 'unknown'} · "
            f"size {record.get('size', 0)} · "
            f"units {record.get('units_done', 0)}/"
            f"{record.get('units_total', 0)} · "
            f"after {', '.join(after) if after else 'nothing'}{added}",
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

    A supervisor receives its prompt as its first message, so this file is
    not how a relaunch delivers one. It is the standing address of the
    front's newest prompt — rewritten on every summon and every relaunch —
    for a session whose own copy has aged out of its context, and for
    anyone reading what a live supervisor was actually told.
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


def predecessor_block(session_id: str | None,
                      relaunched: bool = False) -> str:
    """The last checkpoint to pick up from, or the fresh-start line.

    A relaunch keeps the Foreman session id and starts a fresh vendor
    conversation, so the checkpoint being replayed is the session's own,
    written before it was relaunched. It is named that way rather than as
    somebody else's: a supervisor told it replaces itself would read its
    own words as a stranger's.
    """
    if not session_id:
        return ("(none: you are the first supervisor summoned for this "
                "front, so there is nothing to pick up.)")
    record = store.read_snapshot(paths.checkpoint_path(session_id),
                                 default=None)
    whose = ("You were relaunched: this conversation is fresh, your session "
             "id is not"
             if relaunched else f"Session {session_id}, which you replace,")
    if not isinstance(record, dict):
        if relaunched:
            return (f"{whose}, and session {session_id} left no checkpoint "
                    f"before the relaunch. Read the front ledger and the "
                    f"brief, then checkpoint what you find before you plan "
                    f"anything.")
        return (f"{whose} left no checkpoint. Read the front ledger and the "
                f"brief, then checkpoint what you find before you plan "
                f"anything.")
    held = [str(item) for item in (record.get("held") or [])]
    questions = [str(item) for item in (record.get("questions") or [])]
    opening = (f"{whose}. This is the checkpoint you left before the "
               f"relaunch, and it is where you pick up:"
               if relaunched
               else f"{whose} left this. It is where you pick up:")
    return (
        f"{opening}\n\n"
        f"- doing: {record.get('doing') or '(not declared)'}\n"
        f"- next: {record.get('next') or '(not declared)'}\n"
        f"- held: {', '.join(held) if held else 'nothing'}\n"
        f"- open questions: "
        f"{', '.join(questions) if questions else 'none'}"
    )


#: The headless variant of the supervisor prompt: one event, act,
#: checkpoint, end the turn. Same fields as the windowed template, so the
#: same mapping renders both.
SUPERVISOR_HEADLESS_TEMPLATE = "supervisor-headless"
#: The headless variant of the merge-desk prompt. Same fields as the
#: windowed desk template.
MERGE_DESK_HEADLESS_TEMPLATE = "merge-desk-headless"


def render_supervisor_prompt(*, front: str, record: dict, session_id: str,
                             repo: str, branch: str, role_prompt: Path,
                             predecessor: str | None,
                             relaunched: bool = False,
                             headless: bool = False) -> str:
    from .collector import DEFAULTS

    silent = float(DEFAULTS["supervisor_silent_seconds"]) / 60
    template = SUPERVISOR_HEADLESS_TEMPLATE if headless else SUPERVISOR
    return render_role_template(template, {
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
        "predecessor": predecessor_block(predecessor, relaunched),
        "front_prompt": str(front_prompt_path(front)),
        "environment": supervisor_environment_block(
            front=front, repo=repo, branch=branch, session_id=session_id,
            role_prompt=str(role_prompt),
            checkpoint=str(paths.checkpoint_path(session_id))),
    })


def publish_front_prompt(front: str, text: str) -> Path:
    """Publish the newest prompt at the front's standing address."""
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
                        launched_by: str | None,
                        vendor_id: str | None = None,
                        headless: bool = False,
                        model: str | None = None) -> Session:
    return Session(
        id=session_id,
        role=SUPERVISOR,
        pool=SUPERVISOR_POOL,
        model=model or SUPERVISOR_MODEL,
        front=front,
        job=None,
        worktree=repo,
        log=str(paths.session_log_path(session_id)),
        launched_by=launched_by,
        vendor_session=vendor_id,
        headless=headless,
        started_at=store.utcnow_iso(),
        state="starting",
    )


def _start_supervisor(session_id: str, *, repo: str, role_prompt: Path,
                      vendor_id: str, workspace: str | None,
                      model: str | None = None
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
    except Refused as exc:
        return argv, inner, None, str(exc)
    except OSError as exc:
        return argv, inner, None, f"OSError: cannot write MCP config: {exc}"
    # A relaunch reuses the session id, so the pid file may still hold the
    # pid of the process just stopped. `spawn_and_wait` returns the first
    # pid it reads there, and a stale one would put a dead process on the
    # roster as this launch's own — recorded running, watched, never alive.
    try:
        pid_path.unlink()
    except OSError:
        pass
    inner = supervisor_inner_command(
        pid_path=pid_path, session_id=session_id, repo=repo,
        role_prompt=role_prompt, vendor_id=vendor_id,
        mcp_config=mcp_config, model=model)
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


def _print_probed_mcp(mcp_config: Path) -> None:
    """The config path, then which interpreter the probe accepted."""
    print(f"mcp config: {mcp_config}")
    try:
        payload = json.loads(mcp_config.read_text(encoding="utf-8"))
        command = payload["mcpServers"]["foreman"]["command"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return
    if isinstance(command, str) and command:
        print(f"mcp: {command} (probed)")


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
        _print_probed_mcp(mcp_config)
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


def _print_headless(session_id: str, vendor_id: str | None,
                      role_prompt: Path, vendor_argv: list[str],
                      outer_argv: list[str],
                      front: str | None = None,
                      branch: str | None = None,
                      mcp_config: Path | None = None) -> None:
    """Report a headless summon: rostered with no window and no process."""
    from . import headless as headless_module

    print(f"session: {session_id}")
    print(f"vendor session: {vendor_id or '(none yet — recorded off the first turn stream)'}")
    print(f"role prompt: {role_prompt}")
    if mcp_config is not None:
        _print_probed_mcp(mcp_config)
    if front:
        print(f"front prompt: {front_prompt_path(front)}")
    if branch:
        print(f"branch: {branch}")
    print("window: none (headless — one turn per wake, no long-lived process)")
    print("pid: (none — a headless session holds no process between turns)")
    inner = headless_module.turn_script_inner(
        vendor_argv, session_id=session_id)
    print(f"command: {_common.printable_command(outer_argv, inner)}")
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
    """`foreman launch supervisor <front> [--workspace N] [--headless] [--dry-run]`."""
    from . import headless as headless_module

    model = _resolve_summoned_model(args)
    front = args.pool
    headless = headless_module.resolve_launch_headless(args, problems)
    if headless and getattr(args, "workspace", None) is not None:
        problems.append(
            f"--workspace {args.workspace!r} names a window workspace, but "
            f"a headless session has no window and no workspace; "
            f"drop --workspace")
    workspace = None if headless \
        else _resolve_window_workspace(args.workspace, problems)
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
            f"one supervisor. To replace it: "
            f"`foreman kill {held} --reason <why>`, then "
            f"`foreman relaunch {held}`")
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
    if headless:
        return launch_supervisor_headless(
            args, front=front, record=record, repo=repo, branch=branch)

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
        except Refused as exc:
            return refuse(str(exc))
        except OSError as exc:
            return refuse(f"cannot write launch files: {exc.strerror or exc}")
        inner = supervisor_inner_command(
            pid_path=paths.session_pid_path(session_id),
            session_id=session_id, repo=repo, role_prompt=role_prompt,
            vendor_id=vendor_id, mcp_config=mcp_config, model=model)
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
                                    os.environ.get(caller.SESSION_ENV),
                                    vendor_id, model=model)),
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
            vendor_id=vendor_id, workspace=workspace, model=model)
    except Exception as exc:  # noqa: BLE001 - anything here is a refusal
        failure = f"{type(exc).__name__}: {exc}"
    starttime, dead = _confirm_started(pid)
    if starttime is None:
        _record_failed(session_id)
        return refuse(f"the window launcher failed to start the supervisor: "
                      f"{failure or dead}")
    assert pid is not None
    _record_running(session_id, pid, starttime)
    # The front record names its supervisor the way `front take` writes it:
    # the screen resolves through that field first, so it never reads "no
    # checkpoint yet" for a session this call just rostered. Bookkeeping,
    # never the launch: a process that exists with no record write still
    # supervises, so a failed write warns and the summon stands.
    try:
        fronts.set_front_supervisor(
            front, session_id,
            by=os.environ.get(caller.SESSION_ENV) or caller.OWNER)
    except OSError as exc:
        print(f"foreman launch: warning: cannot record the supervisor on "
              f"front '{front}': {exc.strerror or exc}", file=sys.stderr)
    from . import mcp as mcp_module

    _print_supervisor(session_id, vendor_id, role_prompt, workspace, pid,
                      argv, inner, front=front, branch=branch,
                      mcp_config=mcp_module.mcp_config_path(session_id))
    hooks.fire("on-launch", {"session": session_id, "role": "supervisor",
                             "front": front, "branch": branch})
    return 0


def launch_supervisor_headless(args: argparse.Namespace, *, front: str,
                               record: dict, repo: str,
                               branch: str) -> int:
    """Mint a headless supervisor: rostered with no window, first turn now.

    The session is recorded before its first turn runs, so a dead first
    turn still leaves a record, never an orphan. The first turn runs
    synchronously under its transient unit; its vendor session id is
    recorded off the stream. A first turn that dies twice is retried
    with the same prompt and then raised as an anomaly, and the launch
    is refused — the session stays rostered for the next wake either
    way.
    """
    from . import headless as headless_module
    from . import mcp as mcp_module

    model = _resolve_summoned_model(args)
    session_id = ids.mint("session")
    session_dir = paths.session_dir(session_id)
    role_prompt = session_dir / ROLE_PROMPT_FILE
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        text = render_supervisor_prompt(
            front=front, record=record, session_id=session_id, repo=repo,
            branch=branch, role_prompt=role_prompt, predecessor=None,
            headless=True)
        write_no_symlink(str(role_prompt), text)
        mcp_config = mcp_module.write_mcp_config(session_id)
    except Refused as exc:
        return refuse(str(exc))
    except OSError as exc:
        return refuse(f"cannot write launch files: {exc.strerror or exc}")

    vendor_argv = headless_module.first_turn_vendor_argv(
        prompt_text=text, mcp_config=mcp_config, model=model)
    outer_argv = headless_module.turn_outer_argv(
        session_id, session_dir / RUN_SCRIPT_FILE, repo=repo)
    if args.dry_run:
        _print_headless(session_id, None, role_prompt, vendor_argv,
                        outer_argv, front=front, branch=branch,
                        mcp_config=mcp_config)
        print()
        print(text)
        return 0

    try:
        store.update_snapshot(
            paths.roster_path(),
            lambda roster: _place_session(
                roster,
                _supervisor_session(session_id, front, repo,
                                    os.environ.get(caller.SESSION_ENV),
                                    headless=True, model=model)),
            default={"sessions": {}},
        )
    except OSError as exc:
        return refuse(f"cannot record the session on the roster: "
                      f"{exc.strerror or exc}")

    try:
        publish_front_prompt(front, text)
    except (Refused, OSError) as exc:
        _record_failed(session_id)
        return refuse(f"cannot publish the front prompt: {exc}")

    first = headless_module.run_first_turn(
        session_id, prompt_text=text, repo=repo, mcp_config=mcp_config,
        model=model)
    vendor_id = first["vendor_session"]
    if vendor_id is not None:
        try:
            headless_module.record_vendor_session(session_id, vendor_id)
        except OSError as exc:
            _record_failed(session_id)
            return refuse(f"cannot record the vendor session: "
                          f"{exc.strerror or exc}")
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _move_session(roster, session_id, state="running",
                                     pid=None, pgid=None),
        default={"sessions": {}},
    )
    try:
        fronts.set_front_supervisor(
            front, session_id,
            by=os.environ.get(caller.SESSION_ENV) or caller.OWNER)
    except OSError as exc:
        print(f"foreman launch: warning: cannot record the supervisor on "
              f"front '{front}': {exc.strerror or exc}", file=sys.stderr)
    _print_headless(session_id, vendor_id, role_prompt, vendor_argv,
                    outer_argv, front=front, branch=branch,
                    mcp_config=mcp_config)
    hooks.fire("on-launch", {"session": session_id, "role": "supervisor",
                             "front": front, "branch": branch,
                             "headless": True})
    if not first["ok"]:
        exits = [attempt["exit_code"] if not attempt["timed_out"]
                 else "timeout" for attempt in first["attempts"]]
        headless_module.raise_turn_anomaly(session_id, [], exits)
        return refuse(
            f"the first turn failed twice (exits "
            f"{', '.join(str(exit) for exit in exits)}); the session "
            f"'{session_id}' stays rostered for its next wake")
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
                             role_prompt: Path,
                             headless: bool = False) -> str:
    template = MERGE_DESK_HEADLESS_TEMPLATE if headless else MERGE_DESK
    return render_role_template(template, {
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
                        launched_by: str | None,
                        headless: bool = False,
                        model: str | None = None) -> Session:
    return Session(
        id=session_id,
        role=MERGE_DESK,
        pool=SUPERVISOR_POOL,
        model=model or SUPERVISOR_MODEL,
        front=None,
        job=None,
        worktree=repo,
        log=str(paths.session_log_path(session_id)),
        launched_by=launched_by,
        headless=headless,
        started_at=store.utcnow_iso(),
        state="starting",
    )


def launch_merge_desk_main(args: argparse.Namespace,
                           problems: list[str]) -> int:
    """`foreman launch merge-desk [--workspace N] [--headless] [--dry-run]`."""
    from . import headless as headless_module

    model = _resolve_summoned_model(args)
    if args.pool is not None:
        problems.append(
            f"a merge desk takes no pool (got {args.pool!r}): it is the "
            f"desk, not work on one (`foreman launch merge-desk`)")
    if args.spec is not None:
        problems.append(
            "a merge desk takes no spec file: it works the merge queue in "
            "the repository")
    headless = headless_module.resolve_launch_headless(args, problems)
    if headless and getattr(args, "workspace", None) is not None:
        problems.append(
            f"--workspace {args.workspace!r} names a window workspace, but "
            f"a headless session has no window and no workspace; "
            f"drop --workspace")
    workspace = None if headless \
        else _resolve_window_workspace(args.workspace, problems)
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
    if headless:
        return launch_merge_desk_headless(
            args, repo=repo, branch=branch)

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
            vendor_id=vendor_id, model=model)
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
                                    os.environ.get(caller.SESSION_ENV),
                                    model=model)),
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
            vendor_id=vendor_id, workspace=workspace, model=model)
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


def launch_merge_desk_headless(args: argparse.Namespace, *, repo: str,
                               branch: str) -> int:
    """Mint a headless merge desk: rostered with no window, first turn now.

    The desk owns no front, so its prompt carries the merge queue instead
    of a front's tasks; otherwise the shape is the supervisor's — record
    first, run the first turn under its transient unit, record the vendor
    session id off the stream.
    """
    from . import headless as headless_module
    from . import mcp as mcp_module

    model = _resolve_summoned_model(args)
    session_id = ids.mint("session")
    session_dir = paths.session_dir(session_id)
    role_prompt = session_dir / ROLE_PROMPT_FILE
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        text = render_merge_desk_prompt(
            session_id=session_id, repo=repo, branch=branch,
            role_prompt=role_prompt, headless=True)
        write_no_symlink(str(role_prompt), text)
        mcp_config = mcp_module.write_mcp_config(session_id)
    except Refused as exc:
        return refuse(str(exc))
    except OSError as exc:
        return refuse(f"cannot write launch files: {exc.strerror or exc}")

    vendor_argv = headless_module.first_turn_vendor_argv(
        prompt_text=text, mcp_config=mcp_config, model=model)
    outer_argv = headless_module.turn_outer_argv(
        session_id, session_dir / RUN_SCRIPT_FILE, repo=repo)
    if args.dry_run:
        _print_headless(session_id, None, role_prompt, vendor_argv,
                        outer_argv, branch=branch, mcp_config=mcp_config)
        print()
        print(text)
        return 0

    try:
        store.update_snapshot(
            paths.roster_path(),
            lambda roster: _place_session(
                roster,
                _merge_desk_session(session_id, repo,
                                    os.environ.get(caller.SESSION_ENV),
                                    headless=True, model=model)),
            default={"sessions": {}},
        )
    except OSError as exc:
        return refuse(f"cannot record the session on the roster: "
                      f"{exc.strerror or exc}")

    first = headless_module.run_first_turn(
        session_id, prompt_text=text, repo=repo, mcp_config=mcp_config,
        model=model)
    vendor_id = first["vendor_session"]
    if vendor_id is not None:
        try:
            headless_module.record_vendor_session(session_id, vendor_id)
        except OSError as exc:
            _record_failed(session_id)
            return refuse(f"cannot record the vendor session: "
                          f"{exc.strerror or exc}")
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _move_session(roster, session_id, state="running",
                                     pid=None, pgid=None),
        default={"sessions": {}},
    )
    _print_headless(session_id, vendor_id, role_prompt, vendor_argv,
                    outer_argv, branch=branch, mcp_config=mcp_config)
    hooks.fire("on-launch", {"session": session_id, "role": "merge-desk",
                             "headless": True})
    if not first["ok"]:
        exits = [attempt["exit_code"] if not attempt["timed_out"]
                 else "timeout" for attempt in first["attempts"]]
        headless_module.raise_turn_anomaly(session_id, [], exits)
        return refuse(
            f"the first turn failed twice (exits "
            f"{', '.join(str(exit) for exit in exits)}); the session "
            f"'{session_id}' stays rostered for its next wake")
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
                     launched_by: str | None,
                     model: str | None = None) -> Session:
    return Session(
        id=session_id,
        role=FOREMAN,
        pool=SUPERVISOR_POOL,
        model=model or SUPERVISOR_MODEL,
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
    model = _resolve_summoned_model(args)
    if getattr(args, "headless", False):
        problems.append(
            "the foreman stays interactive: `foreman launch foreman "
            "--headless` is refused; only supervisors and the merge desk "
            "run headless")
    if args.pool is not None:
        problems.append(
            f"a foreman takes no pool (got {args.pool!r}): it holds the "
            f"swarm, not work on one (`foreman launch foreman`)")
    if args.spec is not None:
        problems.append(
            "a foreman takes no spec file: it works the front queue in "
            "the repository")
    workspace = _resolve_window_workspace(args.workspace, problems)
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
            vendor_id=vendor_id, model=model)
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
                                 os.environ.get(caller.SESSION_ENV),
                                 model=model)),
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
            vendor_id=vendor_id, workspace=workspace, model=model)
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
        vendor_session=str(args.session).strip() if args.session else None,
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
# `foreman relaunch`: the same Foreman session, a fresh conversation.
#
# A relaunch never resumes. `claude --resume` takes no positional prompt, so
# a resumed supervisor could only be told to go and read a file, and it did
# not: the process sat idle while its front waited. So the old process is
# stopped and a new vendor conversation is summoned under the same Foreman
# session id, with a freshly rendered role prompt as its first message and
# the session's own last checkpoint inside it. The roster keeps one record
# and records the new vendor id on it.
# --------------------------------------------------------------------------


def add_relaunch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session", help="the session to summon anew")
    parser.add_argument("--workspace", default=None,
                        help="workspace to open the window on")
    parser.add_argument("--repo", default=None, help="repository to work in")
    parser.add_argument("--branch", default=None, help="the front's branch")
    parser.add_argument("--dry-run", action="store_true",
                        help="render the prompt and print the command; start nothing")


def relaunch_headless_main(session_id: str, *, old: dict,
                             problems: list[str],
                             workspace: str | None = None,
                             dry_run: bool = False,
                             by: str | None = None,
                             quiet: bool = False) -> int:
    """Relaunch a headless session as a wake, never as a new process."""
    from . import headless as headless_module

    def say(text: str = "") -> None:
        if not quiet:
            print(text)

    if workspace is not None:
        problems.append(
            f"--workspace {workspace!r} names a window workspace, but a "
            f"headless session has no window and no workspace; "
            f"drop --workspace")
    role = old.get("role") or ""
    front = old.get("front")
    if role == SUPERVISOR:
        record = fronts.read_front_record(front) if front else None
        if record is None:
            problems.append(
                f"session '{session_id}' names front '{front or '(none)'}', "
                f"which has no record on the ledger")
    if (old.get("state") or "") not in ("starting", "running"):
        problems.append(
            f"session '{session_id}' is {old.get('state') or 'in no state'}; "
            f"only a live headless session is relaunched as a wake")
    if problems:
        return refuse(*problems)
    if dry_run:
        say(f"relaunches: {session_id} "
            f"(headless — queued as a wake, no new process)")
        say(f"event text: {headless_module.RELAUNCH_TEXT}")
        return 0
    event = headless_module.relaunch_wake(session_id, by=by)
    say(f"relaunches: {session_id} "
        f"(headless — queued as a wake, no new process)")
    say(f"event: {event['id']}")
    return 0


def relaunch_main(session_id: str, *, workspace: str | None = None,
                  repo: str | None = None, branch: str | None = None,
                  dry_run: bool = False, by: str | None = None,
                  quiet: bool = False) -> int:
    """Summon one supervisor anew. The verb and the collector share this.

    ``by`` names who asked (a session id, or ``collector`` when the daemon
    did it) and lands on the roster; ``quiet`` silences the report so the
    daemon writes records rather than a screen. Every refusal still names
    itself on stderr: a relaunch that did not happen must not be silent.
    """
    def say(text: str = "") -> None:
        if not quiet:
            print(text)

    problems: list[str] = []
    frozen = paths.frozen_path()
    if frozen.exists():
        problems.append(
            f"frozen file {frozen} exists; the launcher refuses while frozen")
    # The role gate lives in `cmd_relaunch`: the collector calls this
    # function directly and holds no session, so gating here would refuse
    # the one caller flow 4 depends on.
    sessions = caller.read_roster().get("sessions", {})
    old = sessions.get(session_id)
    if not isinstance(old, dict):
        problems.append(
            f"unknown session '{session_id}'; the roster has no record of it")
        old = {}
    elif (old.get("role") or "") != SUPERVISOR and not (
            bool(old.get("headless"))
            and (old.get("role") or "") == MERGE_DESK):
        problems.append(
            f"session '{session_id}' has role '{old.get('role') or '(none)'}'; "
            f"only a supervisor is relaunched")
    # A headless relaunch is not a new process shape: it is a wake
    # carrying "you were relaunched, read your checkpoint". No window, no
    # workspace, no vendor conversation to stop or summon.
    if old.get("headless"):
        return relaunch_headless_main(
            session_id, old=old, problems=problems, workspace=workspace,
            dry_run=dry_run, by=by, quiet=quiet)
    # The workspace is resolved for every windowed caller — the daemon's
    # relaunch needs a window as much as the verb's does, and a windowed
    # launch with nowhere to put its window is refused by name rather
    # than left to time out.
    workspace = _resolve_window_workspace(workspace, problems)

    front = old.get("front")
    record = fronts.read_front_record(front) if front else None
    if old and record is None:
        problems.append(
            f"session '{session_id}' names front '{front or '(none)'}', "
            f"which has no record on the ledger")
    where = os.path.abspath(repo or old.get("worktree") or os.getcwd())
    if not os.path.isdir(where):
        problems.append(f"repo {where!r} is not a directory")
    on_branch = _branch_of_checkout(where, branch, problems)
    # Another live supervisor of the same front is the one thing a relaunch
    # must not add to. The session being relaunched is itself, so it is not
    # counted against itself.
    if os.path.isdir(where) and on_branch is not None:
        dirty = dirty_tree_problem(where)
        if dirty is not None:
            problems.append(dirty)
    other = live_front_supervisor(front, ignore=session_id) if old else None
    if other is not None:
        held, entry = other
        problems.append(
            f"front '{front}' already has another live supervisor '{held}' "
            f"({entry.get('state')}, pid {entry.get('pid')}); one front has "
            f"one supervisor, so relaunch that one instead")
    if problems:
        return refuse(*problems)
    assert record is not None and on_branch is not None
    recorded_model = old.get("model")
    model = recorded_model if isinstance(recorded_model, str) and recorded_model \
        else SUPERVISOR_MODEL

    # A fresh conversation, so a fresh vendor id: the old one is the
    # transcript of a session that is being stopped, and nothing resumes it.
    vendor_id = str(uuid.uuid4())
    session_dir = paths.session_dir(session_id)
    role_prompt = session_dir / ROLE_PROMPT_FILE
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        text = render_supervisor_prompt(
            front=front, record=record, session_id=session_id, repo=where,
            branch=on_branch, role_prompt=role_prompt,
            predecessor=session_id, relaunched=True)
    except Refused as exc:
        return refuse(str(exc))
    except OSError as exc:
        return refuse(f"cannot write launch files: {exc.strerror or exc}")

    old_pid = old.get("pid")
    old_vendor = read_vendor_session(session_id)
    old_alive = procs.same_process(old_pid, old.get("pid_starttime"))
    if dry_run:
        from . import mcp as mcp_module

        try:
            mcp_config = mcp_module.write_mcp_config(session_id)
        except Refused as exc:
            return refuse(str(exc))
        except OSError as exc:
            return refuse(f"cannot write launch files: {exc.strerror or exc}")
        inner = supervisor_inner_command(
            pid_path=paths.session_pid_path(session_id),
            session_id=session_id, repo=where, role_prompt=role_prompt,
            vendor_id=vendor_id, mcp_config=mcp_config, model=model)
        argv = supervisor_outer_argv(
            session_id, session_dir / RUN_SCRIPT_FILE, workspace)
        say(f"relaunches: {session_id}")
        say(f"replaces vendor session: {old_vendor or '(none recorded)'}")
        say(f"would stop: pid {old_pid}" if old_alive
            else f"would stop: nothing ({session_id} is not running)")
        _print_supervisor(session_id, vendor_id, role_prompt, workspace,
                          None, argv, inner, front=front, branch=on_branch,
                          mcp_config=mcp_config)
        if not quiet:
            print()
            print(text)
        return 0

    # The old process is stopped before the new one is started, and the
    # roster says what was stopped. Two live supervisors of one front, both
    # authorised to plan and dispatch, is the failure this runtime exists to
    # prevent, and authority follows the process — so the process ends first.
    stopped: list[int] = []
    if old_alive:
        signalled, remaining = procs.kill_job(old_pid, old.get("pgid"))
        if remaining:
            return refuse(
                f"session '{session_id}' is still alive after being stopped "
                f"(pids {sorted(remaining)}); nothing was summoned in its "
                f"place, because two live supervisors of one front is the "
                f"failure this runtime exists to prevent")
        stopped = signalled
    now = store.utcnow_iso()
    relaunches = old.get("relaunches")
    relaunches = relaunches + 1 if isinstance(relaunches, int) else 1
    try:
        write_no_symlink(str(role_prompt), text)
        _write_vendor_session(session_id, vendor_id)
    except Refused as exc:
        return refuse(str(exc))
    except OSError as exc:
        return refuse(f"cannot write launch files: {exc.strerror or exc}")
    # One record, moved back to `starting`: the session id is the same one,
    # so its checkpoint, its slot grants and every anomaly already written
    # about it still name the session that is now running. The counters the
    # collector reads are cleared with it — a fresh conversation that
    # inherited its predecessor's last declared write would be read as
    # silent from its first second.
    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _move_session(
            roster, session_id, state="starting", pid=None, pgid=None,
            pid_starttime=None, worktree=where,
            vendor_session=vendor_id,
            started_at=now, last_declared_at=None, last_observed_at=None,
            cpu_s=0.0,
            relaunches=relaunches, relaunched_at=now,
            relaunched_by=by or caller.OWNER,
            replaced_vendor_session=old_vendor,
            stopped_pid=old_pid if old_alive else None,
            stopped_pids=stopped,
            stopped_at=now if old_alive else None),
        default={"sessions": {}},
    )

    argv: list[str] = []
    inner = ""
    pid: int | None = None
    failure: str | None = None
    try:
        publish_front_prompt(front, text)
        argv, inner, pid, failure = _start_supervisor(
            session_id, repo=where, role_prompt=role_prompt,
            vendor_id=vendor_id, workspace=workspace, model=model)
    except Exception as exc:  # noqa: BLE001 - anything here is a refusal
        failure = f"{type(exc).__name__}: {exc}"
    starttime, dead = _confirm_started(pid)
    if starttime is None:
        _record_failed(session_id)
        return refuse(f"the window launcher failed to start the supervisor: "
                      f"{failure or dead}")
    assert pid is not None
    _record_running(session_id, pid, starttime)
    say(f"relaunches: {session_id}")
    say(f"replaces vendor session: {old_vendor or '(none recorded)'}")
    say(f"stopped: pid {old_pid} ({len(stopped)} process(es))" if stopped
        else f"stopped: nothing ({session_id} was not running)")
    from . import mcp as mcp_module

    if not quiet:
        _print_supervisor(session_id, vendor_id, role_prompt, workspace, pid,
                          argv, inner, front=front, branch=on_branch,
                          mcp_config=mcp_module.mcp_config_path(session_id))
    hooks.fire("on-launch", {"session": session_id, "role": "supervisor",
                             "front": front, "branch": on_branch,
                             "relaunch": True})
    return 0


@subcommand("relaunch",
            help="Stop a supervisor and summon it anew from its checkpoint.")
def cmd_relaunch(args: argparse.Namespace) -> int:
    me, violations = caller.resolve("relaunch")
    caller.check_role(me, "relaunch", FOREMAN, SUPERVISOR,
                      violations=violations)
    if violations:
        return refuse(*violations)
    return relaunch_main(args.session, workspace=args.workspace,
                         repo=args.repo, branch=args.branch,
                         dry_run=args.dry_run, by=caller.by_line(me))


cmd_relaunch.add_arguments = add_relaunch_arguments  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# `foreman kill`: end what the launcher started, and say who and why.
#
# The unit first, then the pid: a worker runs as a transient systemd unit
# whose main process is its shell, so stopping the unit reaps the whole
# cgroup; a supervisor runs in a window and has no unit, so its recorded pid
# and process group are the kill. Nothing here matches a process by pattern
# and nothing is signalled that this runtime did not record starting.
# --------------------------------------------------------------------------


def add_kill_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", help="session id or job id to kill")
    parser.add_argument("--reason", default=None,
                        help="why it is being killed; it lands on the record")


def _kill_target(target: str) -> tuple[str | None, dict | None, str | None,
                                       dict | None, str | None]:
    """Resolve a kill target to (session id, session, front, job, problem).

    A session id names its own record and whatever job it runs; a job id
    names the session the job runs in, which is the process to stop. An id
    that is neither is the refusal.
    """
    from . import progress

    sessions = caller.read_roster().get("sessions", {})
    entry = sessions.get(target)
    if isinstance(entry, dict):
        front = entry.get("front")
        job_id = entry.get("job")
        job = None
        if isinstance(job_id, str) and job_id:
            job_front, job = progress._find_job(job_id)
            front = job_front or front
        return target, entry, front, job, None
    front, job = progress._find_job(target)
    if job is None:
        return None, None, None, None, (
            f"unknown session or job '{target}'; the roster has no session "
            f"with that id and no front's job ledger has that job")
    session_id = job.get("session")
    session_id = session_id if isinstance(session_id, str) and session_id \
        else None
    entry = sessions.get(session_id) if session_id else None
    return (session_id, entry if isinstance(entry, dict) else None, front,
            job, None)


@subcommand("kill",
            help="Stop a session or a job's session and record who and why.")
def cmd_kill(args: argparse.Namespace) -> int:
    verb = "kill"
    me, violations = caller.resolve(verb)
    # The merge desk kills nothing: it holds the queue, not the roster.
    caller.check_role(me, verb, FOREMAN, SUPERVISOR, violations=violations)
    target = (args.target or "").strip()
    if not target:
        violations.append("field 'target' is required")
    reason = (args.reason or "").strip()
    if not reason:
        violations.append("field '--reason' is required for 'kill'")
    session_id, entry, front, job, problem = (
        _kill_target(target) if target else (None, None, None, None, None))
    if problem:
        violations.append(problem)
    if job is not None and session_id is None:
        violations.append(
            f"job '{job.get('id')}' names no session; there is no process "
            f"this runtime started to stop")
    if entry is None and session_id is not None:
        violations.append(
            f"session '{session_id}' is not on the roster; only a session "
            f"this runtime recorded starting is killed")
    if entry is not None and entry.get("state") == "killed":
        violations.append(f"session '{session_id}' is already killed")
    # A supervisor kills only what it launched. The owner and the foreman
    # kill anything; the desk was already refused by role above.
    if entry is not None and me is not None and me.role == SUPERVISOR:
        launched_by = entry.get("launched_by")
        if launched_by != me.session_id:
            violations.append(
                f"session '{session_id}' was launched by "
                f"'{launched_by or '(nobody this runtime recorded)'}', not by "
                f"you; a supervisor kills only what it launched")
    if violations:
        return caller.Refusal(violations).report()
    assert session_id is not None and entry is not None

    caller.check_self_contained(reason, "kill reason")
    who = caller.by_line(me)
    now = store.utcnow_iso()

    # The unit is the kill group where there is one; a supervisor's window
    # has none and reads as "nothing stopped here", which is not a failure.
    unit_stopped = _common.stop_unit(session_id)
    pid = entry.get("pid")
    pid = pid if isinstance(pid, int) and pid > 0 else None
    stopped: list[int] = []
    remaining: set[int] = set()
    was_alive = procs.same_process(pid, entry.get("pid_starttime"))
    if was_alive:
        pgid = entry.get("pgid")
        pgid = pgid if isinstance(pgid, int) and pgid > 0 else pid
        stopped, remaining = procs.kill_job(pid, pgid)
    if remaining:
        return caller.Refusal([
            f"session '{session_id}' is still alive after being stopped "
            f"(pids {sorted(remaining)}); the roster is unchanged, so what "
            f"the screen says is still true"]).report()

    store.update_snapshot(
        paths.roster_path(),
        lambda roster: _move_session(
            roster, session_id, state="killed", pid=None, pgid=None,
            killed_by=who, killed_reason=reason, killed_at=now,
            stopped_pid=pid if was_alive else None,
            stopped_pids=stopped,
            stopped_at=now if was_alive else None),
        default={"sessions": {}},
    )
    # The job line, when this session was running one. A job that already
    # returned, verified or failed keeps that outcome: the process being
    # stopped afterwards does not undo what it produced.
    job_note = "(none)"
    if job is not None and front:
        state = job.get("state")
        if state in ("planned", "queued", "running"):
            store.append_ledger(
                paths.front_jobs_path(front),
                dict(job, state="killed", killed_by=who,
                     killed_reason=reason, killed_at=now),
                session_id=who)
            from . import wake as _wake

            _wake.emit_job_event(
                front, job.get("id"),
                dict(job, state="killed"), "job killed", now)
            job_note = f"{job.get('id')} killed"
        else:
            job_note = (f"{job.get('id')} left {state}: a job that already "
                        f"ended is not rewritten by a kill")
    released = capacity.release_for_session(session_id, now, "killed")

    print(f"killed: {session_id}")
    print(f"role: {entry.get('role') or '(none)'}")
    print(f"front: {front or '(none)'}")
    print(f"unit: {'stopped' if unit_stopped else 'nothing to stop'}")
    print(f"stopped: pid {pid} ({len(stopped)} process(es))" if was_alive
          else f"stopped: nothing ({session_id} was not running)")
    print(f"job: {job_note}")
    print(f"slots released: {released}")
    print(f"by: {who}")
    print(f"reason: {reason}")
    # No hook fires here. The hook events are the ones this runtime already
    # ships (see :mod:`foreman.hooks`), and inventing one for `kill` would
    # be a second deliverable nobody asked for; what a kill did is on the
    # roster and on the job ledger, which is where it is read.
    return 0


cmd_kill.add_arguments = add_kill_arguments  # type: ignore[attr-defined]
