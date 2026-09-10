"""Progress verbs: a task moves because a supervisor said so, with evidence.

``job queue`` appends a revise line that marks a tree job queued and
records why it waits; ``job verify --confirmed`` appends CONFIRMED
evidence, marks the job verified and adds its units to the task;
``job verify --run`` executes the task's verify in a detached worktree
of the job's head and, on exit 0, does the same; ``job fail`` marks
the job failed with a finding on the task; ``job fail --closed``
records a job on a done front as history and writes no finding,
because the task may no longer be on the ledger and the work already
landed; ``job repoint`` moves a finished job onto a different task on
the same front, and when the job was verified its units move with it;
``task built`` and ``task landed`` move the task once every unit is
accounted for; ``evidence`` and ``finding`` append free-standing
records to the front's ledgers. A CONFIRMED claim with no command
behind it is refused: running the command is what turns a claim into
evidence.

Every verb that moves work refuses a caller who is not the front's
supervisor. ``finding`` is the exception: it records something seen and
moves nothing, so any roster supervisor may file one, and the ledger
stamps who. Every state change appends a revised copy of the record —
no byte already written is ever edited.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import (
    caller, capacity, cli, entities, fronts, hooks, ids, node as node_mod,
    paths, store, wait,
)
from .caller import MERGE_DESK, OWNER, SUPERVISOR, Refusal
from .entities import JOB_ROLES

CONFIRMED = "CONFIRMED"
PLAUSIBLE = "PLAUSIBLE"
EVIDENCE_STATUSES = (CONFIRMED, PLAUSIBLE)


def _refuse(violations: list[str]) -> int:
    return Refusal(violations).report()


def _all_fronts() -> list[str]:
    try:
        return sorted(path.name for path in paths.fronts_dir().iterdir()
                      if path.is_dir())
    except OSError:
        return []


def _read_tasks(front: str) -> tuple[list[dict], dict[str, dict]]:
    folded = store.fold_by_id(store.read_ledger(paths.front_tasks_path(front)))
    return folded, {record["id"]: record for record in folded
                    if isinstance(record.get("id"), str)}


def _read_jobs(front: str) -> tuple[list[dict], dict[str, dict]]:
    folded = store.fold_by_id(store.read_ledger(paths.front_jobs_path(front)))
    return folded, {record["id"]: record for record in folded
                    if isinstance(record.get("id"), str)}


def _find_job(job_id: str) -> tuple[str | None, dict | None]:
    """The front and folded record for a job id, or (None, None)."""
    for front in _all_fronts():
        record = _read_jobs(front)[1].get(job_id)
        if record is not None:
            return front, record
    return None, None


def _find_task(ref: str) -> tuple[str | None, dict | None, list[str]]:
    """A task by id or title: (front, record, ambiguous fronts).

    Ids are minted unique, so an id hit wins at once; a title naming tasks
    on several fronts is ambiguous and refused by name.
    """
    title_hits: list[tuple[str, dict]] = []
    for front in _all_fronts():
        tasks, by_id = _read_tasks(front)
        if ref in by_id:
            return front, by_id[ref], []
        for task in tasks:
            if task.get("title") == ref:
                title_hits.append((front, task))
    if len(title_hits) == 1:
        return title_hits[0][0], title_hits[0][1], []
    return None, None, sorted({front for front, _ in title_hits})


def _front_has_record(name: str) -> bool:
    try:
        if store.read_ledger(paths.front_record_path(name)):
            return True
        return bool(store.read_ledger(paths.front_tasks_path(name)))
    except OSError:
        return False


def _front_for_on(on: str) -> tuple[str | None, str | None]:
    """The front an ``evidence --on`` value belongs to, or a violation."""
    if _front_has_record(on):
        return on, None
    front, record = _find_job(on)
    if record is not None:
        return front, None
    front, record, ambiguous = _find_task(on)
    if ambiguous:
        return None, (f"field '--on' is ambiguous ({on!r} names tasks on "
                      f"fronts: {', '.join(ambiguous)})")
    if record is not None:
        return front, None
    return None, f"unknown task, job or front '{on}'"


def _check(me: caller.Caller | None, front: str | None,
           verb: str, violations: list[str]) -> None:
    """The supervisor check when the record names no front to check against.

    :func:`caller.check_front_supervisor` needs the front the record
    belongs to; when the record itself is unknown there is no front, so a
    non-supervisor is refused by role and anyone else keeps only the
    unknown-record violation.
    """
    if front is None:
        if me is not None and me.role != OWNER and me.role != SUPERVISOR:
            violations.append(f"role '{me.role}' may not call '{verb}'")
        return
    caller.check_front_supervisor(me, front, verb, violations=violations)


#: Fields a reset writes onto a task. They are dropped the moment the task
#: moves forward again, so the screen shows a reset only while it is the
#: last thing that happened to the task.
RESET_FIELDS = ("reset_reason", "reset_by", "reset_at")


def _moved(record: dict, **changes: object) -> dict:
    """A revised task record for a forward move: the reset note cleared."""
    revised = dict(record, **changes)
    for field in RESET_FIELDS:
        revised.pop(field, None)
    return revised


def _release_successors(front: str, who: str) -> list[str]:
    """Move waiting tasks whose predecessors are all satisfied to ready.

    This is where the `after-built` decision lives: a predecessor that is
    landed always satisfies the wait, and a built one satisfies it only
    when the brief says `after-built`. No version-one brief expresses that
    qualifier, so today only landed releases a successor — a task becomes
    ready when its predecessor lands, and not before. One pass is exact:
    becoming ready satisfies nobody, only landed does.
    """
    tasks, by_id = _read_tasks(front)
    by_title = {task.get("title"): task for task in tasks
                if isinstance(task.get("title"), str)}
    released = []
    for task in tasks:
        if task.get("state") != "waiting":
            continue
        satisfied = True
        for pred in task.get("after") or []:
            hit = by_id.get(pred, by_title.get(pred))
            if hit is None or hit.get("state") != "landed":
                satisfied = False
                break
        if satisfied:
            store.append_ledger(paths.front_tasks_path(front),
                                _moved(task, state="ready"), session_id=who)
            released.append(task.get("id"))
    return released


def _task_of_job(front: str, record: dict) -> tuple[dict | None, str]:
    """The folded task a job's units belong to, by id then title."""
    ref = record.get("task") or ""
    tasks, by_id = _read_tasks(front)
    if ref in by_id:
        return by_id[ref], ""
    for task in tasks:
        if task.get("title") == ref:
            return task, ""
    return None, (f"job '{record.get('id')}' names unknown task '{ref}'")


def _task_on_front(front: str, ref: str) -> dict | None:
    """The folded task ``ref`` names on ``front``, by id then title."""
    key = (ref or "").strip()
    if not key:
        return None
    tasks, by_id = _read_tasks(front)
    if key in by_id:
        return by_id[key]
    for task in tasks:
        if task.get("title") == key:
            return task
    return None


#: Job states a verify can take as they stand: the worker came back, with
#: or without writing a finish marker.
RETURNED_LIKE = ("returned", "returned-with-work")

#: Job states a verify can take past failure: the worker died or was
#: stopped, and only work left on the branch makes the verify honest.
FAILED_LIKE = ("failed", "killed")


def job_units_count(record: dict) -> int:
    """How many units verifying this job credits: its count.

    Records written before the count change carry a list of unit ids, and
    folding one credits its length — exactly what `job verify` always
    added — so an old job verifies onto its task with nothing silently
    changed. Anything else (missing, a stray string) credits zero.
    """
    units = record.get("units")
    if isinstance(units, bool):
        return 0
    if isinstance(units, int):
        return max(0, units)
    if isinstance(units, (list, tuple)):
        return len(units)
    return 0


def job_branch_moved(record: dict) -> bool:
    """True when the job's branch holds commits past its base: the work
    exists outside any marker or ledger line.

    Anything unreadable — no branch or base recorded, no checkout to ask,
    no git, an unknown ref — answers False, so a failure stays a failure
    until somebody shows the work.
    """
    branch = (record.get("branch") or "").strip() \
        if isinstance(record.get("branch"), str) else ""
    base = (record.get("base") or "").strip() \
        if isinstance(record.get("base"), str) else ""
    if not branch or not base or branch == base:
        return False
    repo = record.get("worktree") \
        if isinstance(record.get("worktree"), str) else ""
    if not repo or not os.path.isdir(repo):
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "rev-list", "--count",
             f"{base}..{branch}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    try:
        return int(proc.stdout.strip()) > 0
    except ValueError:
        return False


def _collector_last_tick() -> str | None:
    """When the collector last ticked, or None where it never did."""
    try:
        observed = store.read_snapshot(paths.observed_path(), default=None)
    except OSError:
        return None
    if isinstance(observed, dict):
        ticked = observed.get("at")
        if isinstance(ticked, str) and ticked:
            return ticked
    return None


def _worker_last_write(record: dict) -> str | None:
    """The worker's last write as an ISO timestamp, or None.

    The newest mtime across the worktree and the session log, the same
    two places the collector watches: a stale collector and an unfinished
    worker read apart here.
    """
    newest: float | None = None

    def consider(moment: object) -> None:
        nonlocal newest
        if isinstance(moment, (int, float)) and not isinstance(moment, bool) \
                and (newest is None or moment > newest):
            newest = moment

    log = record.get("log")
    if isinstance(log, str) and log:
        try:
            consider(os.stat(log).st_mtime)
        except OSError:
            pass
    worktree = record.get("worktree")
    if isinstance(worktree, str) and worktree and os.path.isdir(worktree):
        try:
            consider(os.stat(worktree).st_mtime)
        except OSError:
            pass
        try:
            walker = os.walk(worktree)
        except OSError:
            walker = iter(())
        try:
            for dirpath, _dirnames, filenames in walker:
                for name in filenames:
                    try:
                        consider(os.stat(
                            os.path.join(dirpath, name)).st_mtime)
                    except OSError:
                        continue
        except OSError:
            pass
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()


def _unverifiable_state_violation(key: str, state: object,
                                 record: dict) -> str:
    """Why a job in no verifiable state cannot be verified.

    Names the collector's last tick and the worker's last write, so a
    stale collector reads apart from an unfinished worker.
    """
    ticked = _collector_last_tick() or "unknown"
    wrote = _worker_last_write(record) or "unknown"
    return (f"job '{key}' is '{state}', not 'returned' "
            f"(only a returned job can be verified; "
            f"collector last tick {ticked}; worker last write {wrote})")


def _unreadable_output_file(path: str) -> str | None:
    """A refusal naming ``path`` when it cannot be read, else None."""
    try:
        with open(path, "rb"):
            pass
    except OSError:
        return f"cannot read --output-file {path!r}"
    return None


def _next_copy_n(directory: Path) -> int:
    """The next 1-based index in ``directory``, so a later copy never
    overwrites an earlier one. A missing directory is empty: the first
    copy is 1."""
    try:
        names = [entry.name for entry in directory.iterdir()]
    except OSError:
        return 1
    highest = 0
    for name in names:
        prefix, sep, _rest = name.partition("-")
        if sep and prefix.isdigit():
            highest = max(highest, int(prefix))
    return highest + 1


def _store_output_file(src: str, dest: Path) -> str:
    """Byte-copy ``src`` to ``dest``, creating the parent. Returns the
    copy's absolute path; the ledger stores that path, never the bytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return str(dest.resolve())


def _git_at(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _job_repository(record: dict) -> str:
    """The git repository a job's worktree belongs to.

    ``git worktree add`` must run from the main checkout: a linked
    worktree's common dir sits inside it, so the repository is that
    dir's parent. When the worktree itself is gone, the recorded path
    is all we have.
    """
    worktree = record.get("worktree") \
        if isinstance(record.get("worktree"), str) else ""
    worktree = worktree.strip()
    if worktree and os.path.isdir(worktree):
        proc = _git_at(worktree, "rev-parse", "--git-common-dir")
        common = proc.stdout.strip() if proc.returncode == 0 else ""
        if common:
            common_path = Path(common) if os.path.isabs(common) \
                else Path(worktree) / common
            try:
                return str(common_path.resolve().parent)
            except OSError:
                pass
    return worktree


def _job_head(record: dict, repo: str) -> tuple[str, str]:
    """The sha ``--run`` checks out, or a refusal naming job and branch."""
    stored = record.get("head")
    if isinstance(stored, str) and stored.strip():
        return stored.strip(), ""
    branch = (record.get("branch") or "").strip() \
        if isinstance(record.get("branch"), str) else ""
    key = record.get("id") or ""
    if not branch or not repo or not os.path.isdir(repo):
        return "", f"job '{key}' branch '{branch}' does not resolve"
    proc = _git_at(repo, "rev-parse", "--verify", branch)
    sha = proc.stdout.strip() if proc.returncode == 0 else ""
    if not sha:
        return "", f"job '{key}' branch '{branch}' does not resolve"
    return sha, ""


def _execute_verify_run(record: dict, repo: str, head: str,
                        command: str) -> tuple[int, str, int, str] | None:
    """Run ``command`` in a detached worktree of ``head``.

    Returns ``(exit, output_path, seconds, output_text)``. The worktree
    is gone when this returns. Setup failures refuse and return None.
    """
    key = record.get("id") or ""
    sid = str(record.get("session") or "").strip()
    dest_dir = paths.session_dir(sid) / "verify"
    if not repo or not os.path.isdir(repo):
        _refuse([f"job '{key}' branch "
                 f"'{record.get('branch') or ''}' does not resolve"])
        return None
    child_env = os.environ.copy()
    for name in (caller.SESSION_ENV, paths.STATE_ENV, paths.CONFIG_ENV):
        child_env.pop(name, None)
    area = str(paths.scratch_worktree_dir("verify", key))
    try:
        added = _git_at(repo, "worktree", "add", "--detach", area, head)
        if added.returncode != 0:
            tail = (added.stderr.strip() or added.stdout.strip()).strip()
            _refuse([f"cannot open a verify worktree for '{key}': "
                     f"{tail}".strip()])
            return None
        started = time.monotonic()
        proc = subprocess.run(
            command, shell=True, cwd=area,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=child_env)
        seconds = int(round(time.monotonic() - started))
        output_body = proc.stdout or ""
        exit_code = proc.returncode if proc.returncode is not None else 1
    finally:
        _git_at(repo, "worktree", "remove", "--force", area)
        paths.remove_scratch(area)
    dest = dest_dir / f"{_next_copy_n(dest_dir)}-run.log"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(output_body, encoding="utf-8")
    return exit_code, str(dest.resolve()), seconds, output_body


def job_verify_main(job_id: str, confirmed: bool,
                    command: str | None = None,
                    output: str | None = None,
                    units: str | None = None,
                    because: str | None = None,
                    output_file: str | None = None,
                    run: bool = False) -> int:
    verb = "job verify"
    me, violations = caller.resolve(verb)
    key = (job_id or "").strip()
    if not key:
        violations.append("field 'job' is required")
    front, record = _find_job(key) if key else (None, None)
    if key and record is None:
        violations.append(f"unknown job '{key}'")
    _check(me, front, verb, violations)
    command_text = (command or "").strip()
    output_text = (output or "").strip()
    file_text = (output_file or "").strip()
    if confirmed and run:
        violations.append("give --confirmed or --run, not both")
    if run:
        if command_text or output_text or file_text:
            violations.append(
                "give --run without --command, --output or --output-file")
    else:
        if not confirmed:
            violations.append(
                "field '--confirmed' is required for 'job verify'")
        if not command_text:
            violations.append(
                "field '--command' is required with '--confirmed' "
                "(a claim with no command behind it is not evidence)")
        if output_text and file_text:
            violations.append("give --output or --output-file, not both")
        elif not output_text and not file_text:
            violations.append(
                "field '--output' is required with '--confirmed' "
                "(a claim with no output behind it is not evidence)")
        if file_text:
            unreadable = _unreadable_output_file(file_text)
            if unreadable:
                violations.append(unreadable)
    override: int | None = None
    if units is not None:
        text = units.strip()
        if text.isdigit():
            override = int(text)
        else:
            violations.append(
                f"bad units {units!r}; '--units' takes a count, e.g. "
                f"--units 4")
    because_text = (because or "").strip()
    task = None
    task_violation = ""
    repo = ""
    head = ""
    verify_cmd = ""
    if record is not None:
        state = record.get("state")
        if state == "verified":
            violations.append(f"job '{key}' is already verified")
        elif state in RETURNED_LIKE:
            pass
        elif state in FAILED_LIKE:
            # A job that commits and then dies still did the work: where
            # its branch moved past its base the supervisor verifies it
            # past the failure, saying what the branch holds. Where the
            # branch never moved the refusal stands, with the same two
            # times a running refusal names.
            if not job_branch_moved(record):
                branch = record.get("branch") or "?"
                base = record.get("base") or "?"
                ticked = _collector_last_tick() or "unknown"
                wrote = _worker_last_write(record) or "unknown"
                violations.append(
                    f"job '{key}' is '{state}', not 'returned' "
                    f"(a '{state}' job verifies only with '--because' "
                    f"past a moved branch; branch '{branch}' has not "
                    f"moved past its base '{base}'; "
                    f"collector last tick {ticked}; "
                    f"worker last write {wrote})")
            if not because_text:
                violations.append(
                    f"field '--because' is required to verify a '{state}' "
                    f"job (say what work the branch holds, in one sentence)")
        else:
            violations.append(
                _unverifiable_state_violation(key, state, record))
        # A job that names no task at all is not a broken record: the
        # launcher takes `--front` without `--task`, and work commissioned
        # outside a brief arrives that way. It is verified like any other
        # and adds its units to nothing, because there is nothing for them
        # to be units of. A job naming a task the ledger does not have is
        # still a refusal. `--run` needs the task's verify command, so a
        # job with no task, or a task with an empty verify, is refused.
        if str(record.get("task") or "").strip():
            task, task_violation = _task_of_job(front or "", record)
            if task is None:
                violations.append(task_violation)
            elif run:
                verify_cmd = str(task.get("verify") or "").strip()
                if not verify_cmd:
                    violations.append(
                        f"task '{task.get('title')}' has no verify")
        elif run:
            violations.append(f"job '{key}' names no task")
        if file_text or run:
            sid = record.get("session")
            if not (isinstance(sid, str) and sid.strip()):
                violations.append(f"job '{key}' names no session")
        if run:
            repo = _job_repository(record)
            head, head_err = _job_head(record, repo)
            if head_err:
                violations.append(head_err)
    if violations:
        return _refuse(violations)
    assert front is not None and record is not None
    add = override if override is not None else job_units_count(record)
    done = ((task.get("units_done") or 0) + add) if task is not None else add
    total = (task.get("units_total") or 0) if task is not None else 0
    who = caller.by_line(me)
    now = store.utcnow_iso()
    copied = ""
    run_fields: dict = {}
    extra = ""
    if run:
        ran = _execute_verify_run(record, repo, head, verify_cmd)
        if ran is None:
            return 1
        exit_code, output_path, seconds, output_body = ran
        run_fields = {
            "verify_command": verify_cmd,
            "verify_exit": exit_code,
            "verify_seconds": seconds,
            "verify_output_ref": output_path,
        }
        if exit_code != 0:
            store.append_ledger(paths.front_jobs_path(front),
                                dict(record, **run_fields),
                                session_id=who)
            print(f"{key} verify failed (exit {exit_code} in {seconds}s), "
                  f"output: {output_path}")
            tail = "\n".join((output_body or "").splitlines()[-20:])
            if tail:
                print(tail)
            return 1
        command_text = verify_cmd
        output_ref = output_path
        extra = f" exit 0 in {seconds}s, output: {output_path}"
    elif file_text:
        sid = str(record.get("session") or "").strip()
        dest_dir = paths.session_dir(sid) / "verify"
        basename = Path(file_text).name or "output"
        copied = _store_output_file(
            file_text, dest_dir / f"{_next_copy_n(dest_dir)}-{basename}")
        output_ref = copied
        extra = f" output: {copied}"
    else:
        output_ref = output_text
    store.append_ledger(
        paths.front_evidence_path(front),
        entities.Evidence(on=key, claim=f"job '{key}' verified",
                          status=CONFIRMED, command=command_text,
                          output_ref=output_ref,
                          spec_path=str(record.get("spec_path") or "")
                          ).to_dict(),
        session_id=who,
    )
    # The `--because` sentence travels on the job line, so the screen can
    # show why a job that died still counted.
    revised = dict(record, state="verified", verified_at=now, **run_fields)
    if because_text:
        revised["verify_because"] = because_text
    store.append_ledger(paths.front_jobs_path(front), revised,
                        session_id=who)
    if task is not None:
        store.append_ledger(paths.front_tasks_path(front),
                            _moved(task, units_done=done), session_id=who)
    spec = str(record.get("spec_path") or "")
    if task is not None:
        print(f"{key} verified "
              f"({add} units on task '{task.get('title')}': {done}/{total})"
              f"{extra}")
    else:
        print(f"{key} verified (on front '{front}', no task){extra}")
    if because_text:
        print(f"because: {because_text}")
    if spec:
        # What was verified, not just that something was. A proof run
        # credited a real front's task with a probe job's spec, and nothing
        # on the ledger said which spec had earned it.
        print(f"from spec: {spec}")
    hooks.fire("on-return", {"job": key, "front": front,
                             "outcome": "verified"})
    return 0


def job_fail_main(job_id: str, finding: str | None = None,
                  closed: bool = False) -> int:
    verb = "job fail"
    me, violations = caller.resolve(verb)
    key = (job_id or "").strip()
    if not key:
        violations.append("field 'job' is required")
    front, record = _find_job(key) if key else (None, None)
    if key and record is None:
        violations.append(f"unknown job '{key}'")
    _check(me, front, verb, violations)
    if closed:
        if record is not None and front is not None:
            front_state = (fronts.read_front_record(front) or {}).get("state")
            if front_state != "done":
                violations.append(
                    f"front '{front}' is not done "
                    "(--closed records a job on a closed front as history)")
            state = record.get("state")
            if state == "verified":
                violations.append(f"job '{key}' is already verified "
                                  "(verified work is evidence, not a failure)")
            elif state == "failed":
                violations.append(f"job '{key}' is already failed")
            elif state == "history":
                violations.append(f"job '{key}' is already history")
        if violations:
            return _refuse(violations)
        assert front is not None and record is not None
        who = caller.by_line(me)
        store.append_ledger(paths.front_jobs_path(front),
                            dict(record, state="history"), session_id=who)
        print(f"{key} history")
        return 0
    text = (finding or "").strip()
    if not text:
        violations.append("field '--finding' is required for 'job fail'")
    task = None
    if record is not None:
        state = record.get("state")
        if state == "verified":
            violations.append(f"job '{key}' is already verified "
                              "(verified work is evidence, not a failure)")
        elif state == "failed":
            violations.append(f"job '{key}' is already failed")
        else:
            task, task_violation = _task_of_job(front or "", record)
            if task is None:
                violations.append(task_violation)
    if violations:
        return _refuse(violations)
    assert front is not None and record is not None and task is not None
    caller.check_self_contained(text, "finding title")
    who = caller.by_line(me)
    store.append_ledger(paths.front_jobs_path(front),
                        dict(record, state="failed"), session_id=who)
    store.append_ledger(
        paths.front_findings_path(front),
        entities.Finding(id=ids.mint("finding"),
                         on=task.get("id") or "", class_="failure",
                         title=text, detail=f"job '{key}' failed",
                         evidence_ref="").to_dict(),
        session_id=who,
    )
    print(f"{key} failed")
    hooks.fire("on-return", {"job": key, "front": front,
                             "outcome": "failed"})
    return 0


def job_repoint_main(job_id: str, task: str | None,
                     reason: str | None) -> int:
    """Move a finished job onto a different task on its front.

    A job that named no task, or the wrong one, can be re-pointed once it
    is terminal. A verified job's units leave the old task and land on
    the new one, never below zero. A running job is refused by state;
    only the front's supervisor may call this.
    """
    verb = "job repoint"
    me, violations = caller.resolve(verb)
    key = (job_id or "").strip()
    if not key:
        violations.append("field 'job' is required")
    front, record = _find_job(key) if key else (None, None)
    if key and record is None:
        violations.append(f"unknown job '{key}'")
    _check(me, front, verb, violations)
    reason_text = (reason or "").strip()
    if not reason_text:
        violations.append("field '--reason' is required")
    new_ref = (task or "").strip()
    if not new_ref:
        violations.append("field '--task' is required")
    new_task = None
    if record is not None:
        state = record.get("state")
        if state not in wait.TERMINAL:
            violations.append(f"job '{key}' is '{state}'")
        if front is not None and new_ref:
            new_task = _task_on_front(front, new_ref)
            if new_task is None:
                violations.append(
                    f"--task {new_ref!r} names no task on front {front!r}")
    if violations:
        return _refuse(violations)
    assert front is not None and record is not None and new_task is not None
    who = caller.by_line(me)
    now = store.utcnow_iso()
    old_ref = record.get("task") or ""
    new_id = new_task.get("id") or new_ref
    store.append_ledger(
        paths.front_jobs_path(front),
        dict(record, task=new_id, repointed_from=old_ref,
             repointed_by=who, repointed_at=now,
             repoint_reason=reason_text),
        session_id=who)
    if record.get("state") == "verified":
        units = job_units_count(record)
        old_task, _ = _task_of_job(front, record)
        old_id = old_task.get("id") if old_task is not None else None
        if old_id != new_task.get("id"):
            if old_task is not None:
                old_done = old_task.get("units_done") or 0
                store.append_ledger(
                    paths.front_tasks_path(front),
                    _moved(old_task,
                           units_done=max(0, old_done - units)),
                    session_id=who)
            new_done = new_task.get("units_done") or 0
            store.append_ledger(
                paths.front_tasks_path(front),
                _moved(new_task, units_done=new_done + units),
                session_id=who)
    print(f"job {key}: task {old_ref} -> {new_id}")
    return 0


def _resolve_task(ref: str, violations: list[str],
                  field: str = "task") -> tuple[str | None, dict | None]:
    key = (ref or "").strip()
    if not key:
        violations.append(f"field '{field}' is required")
        return None, None
    front, record, ambiguous = _find_task(key)
    if ambiguous:
        violations.append(f"field '{field}' is ambiguous ({key!r} names tasks "
                          f"on fronts: {', '.join(ambiguous)})")
    elif record is None:
        violations.append(f"unknown task '{key}'")
    return front, record


def task_built_main(task_ref: str, did_myself: str | None = None) -> int:
    """Mark a task built, once every unit is accounted for.

    ``did_myself`` accounts for the units the supervisor did with its own
    hands. Some work has no worker to dispatch it to — a front's proof is
    run by the supervisor, by its brief — and without this the runtime
    could never close the task its own brief assigns to the supervisor: no
    job, no units, no `built`, for work that is finished. It is not a way
    around the evidence: it is refused unless the front carries a
    CONFIRMED evidence record, and what the supervisor says it did travels
    on the task and onto the screen.
    """
    verb = "task built"
    me, violations = caller.resolve(verb)
    front, record = _resolve_task(task_ref, violations)
    _check(me, front, verb, violations)
    by_hand = (did_myself or "").strip()
    if by_hand and front is not None:
        try:
            evidence = store.read_ledger(paths.front_evidence_path(front))
        except OSError:
            evidence = []
        if not any(line.get("status") == CONFIRMED for line in evidence):
            violations.append(
                f"field '--did-myself' needs CONFIRMED evidence on front "
                f"'{front}' first: a unit accounted for by hand is still a "
                "unit somebody has to have checked")
    if record is not None and by_hand and not violations:
        record = dict(record,
                      units_done=max(record.get("units_done") or 0,
                                     record.get("units_total") or 0),
                      built_by_hand=by_hand)
    if record is not None:
        state = record.get("state")
        if state in ("built", "landed"):
            violations.append(f"task '{record.get('title')}' "
                              f"is already '{state}'")
        else:
            done, total = record.get("units_done") or 0, \
                record.get("units_total") or 0
            # An overshoot is fine: a repair job carries its own unit, so a
            # task that needed one ends above its own count.
            if done < total:
                violations.append(
                    f"task '{record.get('title')}' has units {done}/{total} "
                    "done: every unit must be accounted for before 'built'")
    if violations:
        return _refuse(violations)
    assert front is not None and record is not None
    who = caller.by_line(me)
    store.append_ledger(paths.front_tasks_path(front),
                        _moved(record, state="built"), session_id=who)
    released = _release_successors(front, who)
    print(f"{record.get('id')} built"
          + (f" (by hand: {by_hand})" if by_hand else ""))
    for rid in released:
        print(f"{rid} ready")
    return 0


def _open_state(front: str, record: dict) -> str:
    """Where this task sits when no work has been done on it yet.

    Not a constant: a task with a predecessor that has not landed is
    waiting, and one with nothing in front of it is ready. Recomputing is
    what makes a reset the true inverse of the move that built it.
    """
    tasks, by_id = _read_tasks(front)
    by_title = {task.get("title"): task for task in tasks
                if isinstance(task.get("title"), str)}
    for pred in record.get("after") or []:
        hit = by_id.get(pred, by_title.get(pred))
        if hit is None or hit.get("state") != "landed":
            return "waiting"
    return "ready"


def task_reset_main(task_ref: str, reason: str | None = None) -> int:
    """Put a task back where it started, with the reason on the screen.

    A task is moved by evidence, so moving it back is not a correction to
    be made quietly: the reason travels on the record and `foreman status`
    prints it under the task until the task moves again. The units go back
    to zero and a landed head is dropped, because a reset task claims
    nothing. It exists because a proof run marked a real front's first task
    built with a fixture job, and the front that starts on that ledger
    tomorrow must not inherit it.
    """
    verb = "task reset"
    me, violations = caller.resolve(verb)
    front, record = _resolve_task(task_ref, violations)
    _check(me, front, verb, violations)
    why = (reason or "").strip()
    if not why:
        violations.append("field '--reason' is required for 'task reset': "
                          "a task moving backwards is the owner's business")
    if record is not None and front is not None:
        if record.get("state") == _open_state(front, record) and \
                not (record.get("units_done") or 0):
            violations.append(
                f"task '{record.get('title')}' is already "
                f"'{record.get('state')}' with no units done")
    if violations:
        return _refuse(violations)
    assert front is not None and record is not None
    who = caller.by_line(me)
    revised = dict(record, state=_open_state(front, record), units_done=0,
                   reset_reason=why, reset_by=who,
                   reset_at=store.utcnow_iso())
    for gone in ("head", "landed_by", "landed_at"):
        revised.pop(gone, None)
    store.append_ledger(paths.front_tasks_path(front), revised,
                        session_id=who)
    print(f"{record.get('id')} {revised['state']} \u2014 reset: {why}")
    return 0


#: How a front lands its built tasks when its brief does not say. Self is
#: the default by owner ruling: the desk is a product swarm's discipline,
#: and a front that declares no desk must not be stopped from landing by
#: one. A brief says `merge = "desk"` to route through it.
DEFAULT_MERGE_MODE = "self"


def _front_merge_mode(front: str | None) -> str:
    """How this front's built tasks land: "self" or the merge desk.

    A front that declares nothing lands itself. Only a brief that says
    `merge = "desk"` routes through the desk, and Foreman's own fronts
    never do. Defaulting the other way stopped a live front from marking a
    task landed at all, which is a true number missing from the screen.
    """
    if not front:
        return DEFAULT_MERGE_MODE
    try:
        from . import fronts as _fronts
    except ImportError:  # pragma: no cover - the module is always present
        return ""
    record = _fronts.read_front_record(front)
    return (record or {}).get("merge") or DEFAULT_MERGE_MODE


def task_landed_main(task_ref: str, head: str | None = None) -> int:
    verb = "task landed"
    me, violations = caller.resolve(verb)
    front, record = _resolve_task(task_ref, violations)
    if front is not None and _front_merge_mode(front) != "self":
        if me is not None and me.role == SUPERVISOR:
            violations.append(
                f"front '{front}' lands through the merge desk "
                f"(request a merge instead of calling 'task landed')")
        elif me is not None and me.role not in (OWNER, MERGE_DESK):
            violations.append(f"role '{me.role}' may not call '{verb}'")
    else:
        _check(me, front, verb, violations)
    sha = (head or "").strip()
    if not sha:
        violations.append("field '--head' is required for 'task landed'")
    if record is not None and record.get("state") != "built":
        violations.append(f"task '{record.get('title')}' "
                          f"is '{record.get('state')}', not 'built' "
                          "(only a built task can land)")
    if violations:
        return _refuse(violations)
    assert front is not None and record is not None
    who = caller.by_line(me)
    now = store.utcnow_iso()
    store.append_ledger(paths.front_tasks_path(front),
                        _moved(record, state="landed", head=sha,
                               landed_by=who, landed_at=now),
                        session_id=who)
    released = _release_successors(front, who)
    print(f"{record.get('id')} landed {sha}")
    for rid in released:
        print(f"{rid} ready")
    # Merge land lands through this same gate, one task at a time, so one
    # landing is one event no matter which verb the operator called.
    hooks.fire("on-land", {"task": record.get("id"), "front": front,
                           "head": sha})
    return 0


def evidence_main(on: str, claim: str, status: str,
                  command: str | None = None,
                  output: str | None = None) -> int:
    verb = "evidence"
    me, violations = caller.resolve(verb)
    key = (on or "").strip()
    if not key:
        violations.append("field '--on' is required")
    text = (claim or "").strip()
    if not text:
        violations.append("field '--claim' is required")
    state = (status or "").strip()
    if state not in EVIDENCE_STATUSES:
        violations.append("field '--status' must be one of "
                          + ", ".join(EVIDENCE_STATUSES)
                          + (f" (got '{status}')" if status else " (missing)"))
    front, on_violation = _front_for_on(key) if key else (None, None)
    if on_violation is not None:
        violations.append(on_violation)
    _check(me, front, verb, violations)
    if state == CONFIRMED and not (command or "").strip():
        violations.append("field '--command' is required for a CONFIRMED "
                          "claim (a claim with no command behind it is not "
                          "evidence)")
    if violations:
        return _refuse(violations)
    assert front is not None
    stored_on = key
    _, job_record = _find_job(key)
    if job_record is not None:
        stored_on = key
    else:
        _, task_record, _ = _find_task(key)
        if task_record is not None:
            stored_on = task_record.get("id") or key
    caller.check_self_contained(text, "evidence claim")
    who = caller.by_line(me)
    stored = store.append_ledger(
        paths.front_evidence_path(front),
        entities.Evidence(on=stored_on, claim=text, status=state,
                          command=(command or "").strip(),
                          output_ref=(output or "").strip()).to_dict(),
        session_id=who,
    )
    print(f"evidence on {stored_on}")
    print(f"build {store.record_build_commit(stored)}")
    return 0


def finding_main(on: str, class_: str, title: str,
                 detail: str,
                 output_file: str | None = None) -> int:
    verb = "finding"
    me, violations = caller.resolve(verb)
    key = (on or "").strip()
    if not key:
        violations.append("field '--on' is required")
    word = (class_ or "").strip()
    if not word:
        violations.append("field '--class' is required")
    headline = (title or "").strip()
    if not headline:
        violations.append("field '--title' is required")
    body = (detail or "").strip()
    if not body:
        violations.append("field '--detail' is required")
    file_text = (output_file or "").strip()
    if file_text:
        unreadable = _unreadable_output_file(file_text)
        if unreadable:
            violations.append(unreadable)
    front: str | None = None
    stored_on = key
    if key:
        if _front_has_record(key):
            front = key
        elif _find_job(key)[1] is not None:
            violations.append(f"field '--on' must name a task or front "
                              f"(got job '{key}')")
        else:
            task_front, task_record, ambiguous = _find_task(key)
            if ambiguous:
                violations.append(
                    f"field '--on' is ambiguous ({key!r} names tasks on "
                    f"fronts: {', '.join(ambiguous)})")
            elif task_record is None:
                violations.append(f"unknown task or front '{key}'")
            else:
                front, stored_on = task_front, task_record.get("id") or key
    # Any roster supervisor, not only the one who holds this front.
    caller.check_role(me, verb, SUPERVISOR, violations=violations)
    if violations:
        return _refuse(violations)
    assert front is not None
    caller.check_self_contained(headline, "finding title")
    who = caller.by_line(me)
    fid = ids.mint("finding")
    copied = ""
    if file_text:
        dest_dir = paths.session_dir(who) / "findings"
        basename = Path(file_text).name or "output"
        copied = _store_output_file(
            file_text, dest_dir / f"{fid}-{basename}")
    store.append_ledger(
        paths.front_findings_path(front),
        entities.Finding(id=fid, on=stored_on, class_=word, title=headline,
                         detail=body, evidence_ref=copied).to_dict(),
        session_id=who,
    )
    extra = f" evidence: {copied}" if copied else ""
    print(f"{fid} on {stored_on}{extra}")
    return 0


def _job_role_of_node(record: dict | None, node: dict) -> str:
    """The JOB_ROLES name a tree node's team role occupies on this front.

    A node that already names a job role (``grok``) keeps it. A team
    role (``builder``) maps through the front's team entry to that
    entry's pool, then to the job role the pool serves. No mapping is
    an empty string, not a guess: a ceiling keyed by ``builder`` would
    always read as fully held.
    """
    role = str(node.get("role") or "").strip()
    if role in JOB_ROLES:
        return role
    team = record.get("team") if isinstance(record, dict) else None
    if not isinstance(team, list):
        return ""
    for entry in team:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("role") or "") != role:
            continue
        pool = str(entry.get("pool") or "")
        mapped = fronts._job_role_for_pool(pool) if pool else None
        return mapped or pool
    return ""


def _node_job_ids(front: str, node: dict) -> list[str]:
    """Job ids recorded on this node, including earlier revise lines."""
    found: list[str] = []
    seen: set[str] = set()
    current = node.get("job")
    if isinstance(current, str) and current:
        found.append(current)
        seen.add(current)
    nid = node.get("id")
    try:
        lines = store.read_ledger(paths.front_tree_path(front))
    except OSError:
        return found
    for line in lines:
        if not isinstance(line, dict) or line.get("id") != nid:
            continue
        jid = line.get("job")
        if isinstance(jid, str) and jid and jid not in seen:
            seen.add(jid)
            found.append(jid)
    return found


def _node_has_failed_job(front: str, node: dict) -> bool:
    """True when a jobs.jsonl line of this node is ``failed``."""
    job_ids = set(_node_job_ids(front, node))
    if not job_ids:
        return False
    try:
        jobs = store.fold_by_id(store.read_ledger(paths.front_jobs_path(front)))
    except OSError:
        return False
    return any(job.get("id") in job_ids and job.get("state") == "failed"
               for job in jobs)


def compute_node_waits(front: str, node: dict, by_id: dict[str, dict],
                       record: dict | None) -> str:
    """Why a queued job is not starting.

    An ``after`` node that is missing or not ``landed`` is a dependency.
    A ``backup-builder`` waits until a job of this node has failed.
    A ``script`` node skips the slot check and waits ``script runner``.
    Otherwise the front's reserved (or allocated) count for the node's
    job role, compared with what is held, is ``no slot`` when full.
    """
    for dep in node.get("after") or []:
        dep_id = str(dep).strip()
        if not dep_id:
            continue
        other = by_id.get(dep_id)
        if other is None or str(other.get("state") or "") != "landed":
            return f"dependency {dep_id}"
    role = str(node.get("role") or "").strip()
    if role == "backup-builder" and not _node_has_failed_job(front, node):
        return "backup builder: no failed run"
    if role == "script":
        return "script runner"
    job_role = _job_role_of_node(record, node)
    if job_role:
        limit = capacity.ceiling(front, job_role)
        held = capacity.held_by_front_role().get((front, job_role), 0)
        if limit is not None and held >= limit:
            return "no slot"
    return "ready"


def _merge_after(existing: dict, extra: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in list(existing.get("after") or []) + extra:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged


def _write_node_revise(front_name: str, existing: dict, who: str,
                       reason: str, **changes: object) -> dict:
    now = store.utcnow_iso()
    updated = dict(existing)
    updated.update(changes)
    updated["op"] = "revise"
    updated["reason_revised"] = reason
    updated["at"] = now
    updated["by"] = who
    line = entities.Node.from_dict(updated).to_dict()
    line["op"] = "revise"
    line["reason_revised"] = reason
    line["at"] = now
    line["by"] = who
    store.append_ledger(
        paths.front_tree_path(front_name), line, session_id=who)
    return line


def job_queue_main(front: str, node_id: str | None,
                   after: list[str] | None = None,
                   sheet_add: str | None = None) -> int:
    """Queue a tree job. Front supervisor only; never writes ``running``."""
    verb = "job queue"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    _check(me, front_name or None, verb, violations)
    nid = "" if node_id is None else str(node_id).strip()
    if not nid:
        violations.append("field 'node' is required")
    extra_after = [str(item).strip() for item in (after or [])]
    extra_after = [item for item in extra_after if item]
    for raw in after or []:
        if not str(raw).strip():
            violations.append("field '--after' names an empty node id")
    existing: dict | None = None
    by_id: dict[str, dict] = {}
    if record is not None:
        _folded, by_id = node_mod._read_nodes(front_name)
        if nid:
            existing = by_id.get(nid)
            if existing is None:
                violations.append(f"unknown node '{nid}'")
            else:
                kind = str(existing.get("kind") or "")
                if kind != "job":
                    violations.append(
                        f"{nid} is kind {kind}; only a job is queued")
                state = str(existing.get("state") or "")
                if state:
                    violations.append(
                        f"{nid} is {state}; only a job with no state "
                        f"is queued")
        for after_id in extra_after:
            if after_id not in by_id:
                violations.append(
                    f"field '--after' names unknown node '{after_id}'")
    if violations:
        return _refuse(violations)
    assert record is not None and existing is not None
    who = caller.by_line(me)
    now = store.utcnow_iso()
    after_ids = _merge_after(existing, extra_after)
    sheet = "" if sheet_add is None else str(sheet_add)
    queued = dict(existing, after=after_ids, state="queued")
    waits = compute_node_waits(front_name, queued, by_id, record)
    _write_node_revise(
        front_name, existing, who, "queued",
        state="queued", queued_at=now, after=after_ids,
        sheet_add=sheet, waits=waits)
    print(f"queued {nid} (waits: {waits})")
    return 0


def _queue_order(nodes: list[dict]) -> list[dict]:
    """Bumped items newest first, then the rest by queued_at oldest first."""
    bumped = [node for node in nodes if node.get("bumped_at")]
    rest = [node for node in nodes if not node.get("bumped_at")]
    bumped.sort(key=lambda node: str(node.get("bumped_at") or ""),
                reverse=True)
    rest.sort(key=lambda node: str(node.get("queued_at") or ""))
    return bumped + rest


def _load_queued_job(front_name: str, node_id: str | None, verb: str,
                     action: str, me, violations: list[str]
                     ) -> tuple[dict | None, dict | None, dict[str, dict]]:
    """Resolve a queued job for cancel, front or edit.

    Refuses a running, returned or landed node naming the state:
    ``nod-x is running; only a queued job is edited``.
    """
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    nid = "" if node_id is None else str(node_id).strip()
    if not nid:
        violations.append("field 'node' is required")
    existing: dict | None = None
    by_id: dict[str, dict] = {}
    if record is not None and nid:
        _folded, by_id = node_mod._read_nodes(front_name)
        existing = by_id.get(nid)
        if existing is None:
            violations.append(f"unknown node '{nid}'")
        else:
            state = str(existing.get("state") or "")
            if state != "queued":
                shown = state if state else "unstarted"
                violations.append(
                    f"{nid} is {shown}; only a queued job is {action}")
    return record, existing, by_id


def job_cancel_main(front: str, node_id: str | None) -> int:
    verb = "job cancel"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record, existing, _by_id = _load_queued_job(
        front_name, node_id, verb, "cancelled", me, violations)
    if violations:
        return _refuse(violations)
    assert record is not None and existing is not None
    who = caller.by_line(me)
    nid = str(existing["id"])
    _write_node_revise(
        front_name, existing, who, "cancelled", state="cancelled")
    print(f"cancelled {nid}")
    return 0


def job_front_main(front: str, node_id: str | None) -> int:
    verb = "job front"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record, existing, _by_id = _load_queued_job(
        front_name, node_id, verb, "moved to the front", me, violations)
    if violations:
        return _refuse(violations)
    assert record is not None and existing is not None
    who = caller.by_line(me)
    nid = str(existing["id"])
    _write_node_revise(
        front_name, existing, who, "moved to the front",
        bumped_at=store.utcnow_iso())
    print(f"front {nid}")
    return 0


def job_edit_main(front: str, node_id: str | None,
                  what: str | None = None, verify: str | None = None,
                  must_not_touch: str | None = None,
                  after: list[str] | None = None,
                  sheet_add: str | None = None) -> int:
    verb = "job edit"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    extra_after = [str(item).strip() for item in (after or [])]
    extra_after = [item for item in extra_after if item]
    for raw in after or []:
        if not str(raw).strip():
            violations.append("field '--after' names an empty node id")
    record, existing, by_id = _load_queued_job(
        front_name, node_id, verb, "edited", me, violations)
    if record is not None:
        for after_id in extra_after:
            if after_id not in by_id:
                violations.append(
                    f"field '--after' names unknown node '{after_id}'")
    if violations:
        return _refuse(violations)
    assert record is not None and existing is not None
    who = caller.by_line(me)
    nid = str(existing["id"])
    changes: dict[str, object] = {}
    if what is not None:
        changes["what"] = str(what)
    if verify is not None:
        changes["verify"] = str(verify).strip()
    if must_not_touch is not None:
        changes["must_not_touch"] = str(must_not_touch).strip()
    if after is not None:
        changes["after"] = _merge_after(existing, extra_after)
    if sheet_add is not None:
        changes["sheet_add"] = str(sheet_add)
    edited = dict(existing, **changes)
    waits = compute_node_waits(front_name, edited, by_id, record)
    changes["waits"] = waits
    _write_node_revise(front_name, existing, who, "edited", **changes)
    print(f"edited {nid} (waits: {waits})")
    return 0


def job_list_main(front: str) -> int:
    verb = "job list"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    _check(me, front_name or None, verb, violations)
    if violations:
        return _refuse(violations)
    assert record is not None
    folded, _by_id = node_mod._read_nodes(front_name)
    queued = [node for node in folded
              if node.get("kind") == "job"
              and str(node.get("state") or "") == "queued"]
    lines = []
    for node in _queue_order(queued):
        nid = node.get("id")
        team_role = node.get("role") or ""
        job_role = _job_role_of_node(record, node)
        waits = node.get("waits") or ""
        lines.append(f"{nid}  {team_role}  {job_role}  waits: {waits}")
    if lines:
        print("\n".join(lines))
    return 0


def add_job_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="job_verb", required=True)
    queue = verbs.add_parser(
        "queue", help="Queue a tree job; the tree records why it waits.")
    queue.add_argument("front", help="front the node belongs to")
    queue.add_argument("node", help="job node to queue")
    queue.add_argument("--after", action="append", default=None,
                       help="node id this one waits on (repeatable)")
    queue.add_argument("--sheet-add", dest="sheet_add", default=None,
                       help="text added to the job's sheet (stored only)")
    cancel = verbs.add_parser(
        "cancel", help="Cancel a queued tree job.")
    cancel.add_argument("front", help="front the node belongs to")
    cancel.add_argument("node", help="queued job to cancel")
    front_verb = verbs.add_parser(
        "front", help="Move a queued tree job to the front of the queue.")
    front_verb.add_argument("front", help="front the node belongs to")
    front_verb.add_argument("node", help="queued job to bump")
    edit = verbs.add_parser(
        "edit", help="Edit a queued tree job and recompute waits.")
    edit.add_argument("front", help="front the node belongs to")
    edit.add_argument("node", help="queued job to edit")
    edit.add_argument("--what", default=None, help="new body")
    edit.add_argument("--verify", default=None, help="new verify command")
    edit.add_argument("--must-not-touch", dest="must_not_touch",
                      default=None, help="new must-not-touch")
    edit.add_argument("--after", action="append", default=None,
                      help="node id this one waits on (repeatable)")
    edit.add_argument("--sheet-add", dest="sheet_add", default=None,
                      help="text added to the job's sheet (stored only)")
    listing = verbs.add_parser(
        "list", help="Print the front's queued jobs in queue order.")
    listing.add_argument("front", help="front whose queue to print")
    verify = verbs.add_parser("verify", help="Verify a returned job.")
    verify.add_argument("job", help="job id")
    verify.add_argument("--confirmed", action="store_true",
                        help="the supervisor re-ran the verification")
    verify.add_argument("--run", action="store_true",
                        help="run the task's verify in a detached worktree "
                             "of the job's head")
    verify.add_argument("--command", default=None,
                        help="verification command that was re-run (required)")
    verify.add_argument("--output", default=None,
                        help="command output (required unless --output-file)")
    verify.add_argument("--output-file", dest="output_file", default=None,
                        help="path to command output, copied under the "
                             "job's session")
    verify.add_argument("--units", default=None,
                        help="unit count to credit (default: the job's "
                             "launch count)")
    verify.add_argument("--because", default=None,
                        help="one sentence saying what work the branch holds "
                             "(required for a 'failed' or 'killed' job)")
    fail = verbs.add_parser("fail", help="Fail a job with a finding.")
    fail.add_argument("job", help="job id")
    fail.add_argument("--finding", default=None,
                      help="self-contained finding title (required)")
    fail.add_argument("--closed", action="store_true",
                      help="record a job on a done front as history, "
                           "with no finding")
    repoint = verbs.add_parser(
        "repoint", help="Move a finished job onto a different task.")
    repoint.add_argument("job", help="job id")
    repoint.add_argument("--task", default=None,
                         help="task id or title (required)")
    repoint.add_argument("--reason", default=None,
                         help="why the job is being re-pointed (required)")


@cli.subcommand("job", help="Queue, list, verify, fail or repoint a job.")
def _job_entry(args: argparse.Namespace) -> int:
    if args.job_verb == "queue":
        return job_queue_main(args.front, args.node, after=args.after,
                              sheet_add=args.sheet_add)
    if args.job_verb == "cancel":
        return job_cancel_main(args.front, args.node)
    if args.job_verb == "front":
        return job_front_main(args.front, args.node)
    if args.job_verb == "edit":
        return job_edit_main(args.front, args.node, what=args.what,
                             verify=args.verify,
                             must_not_touch=args.must_not_touch,
                             after=args.after, sheet_add=args.sheet_add)
    if args.job_verb == "list":
        return job_list_main(args.front)
    if args.job_verb == "verify":
        return job_verify_main(args.job, args.confirmed,
                               command=args.command, output=args.output,
                               units=args.units, because=args.because,
                               output_file=args.output_file,
                               run=args.run)
    if args.job_verb == "fail":
        return job_fail_main(args.job, finding=args.finding,
                             closed=args.closed)
    if args.job_verb == "repoint":
        return job_repoint_main(args.job, task=args.task,
                                reason=args.reason)
    raise AssertionError(f"unknown job verb {args.job_verb!r}")


_job_entry.add_arguments = add_job_arguments  # type: ignore[attr-defined]


def task_add_main(front: str, title: str | None, scope: str | None,
                  verify: str | None, size: str | int | None,
                  after: list[str] | None) -> int:
    """Append a commissioned task to a running front.

    A supervisor asked to do something the brief did not name hangs it
    under a task here instead of running it task-less: the entry is
    validated exactly as a brief's task is (title, four-part scope,
    verify command, size, after naming real tasks with no cycle) and the
    screen marks it `added by supervisor`. Existing tasks are never
    edited — this only appends.
    """
    verb = "task add"
    me, violations = caller.resolve(verb)
    key = (front or "").strip()
    if not key:
        violations.append("field 'front' is required")
    record = fronts.read_front_record(key) if key else None
    if key and record is None:
        violations.append(f"unknown front '{key}'")
    _check(me, key or None, verb, violations)
    name = (title or "").strip()
    if not name:
        violations.append("field 'title' is required")
    text = (scope or "").strip()
    if not text:
        violations.append("field 'scope' is required")
    check = (verify or "").strip()
    if not check:
        violations.append("field 'verify' is required")
    number: int | None = None
    try:
        number = int(str(size).strip())
    except (TypeError, ValueError, AttributeError):
        violations.append(
            f"field 'size' must be a positive integer (got '{size}')")
    else:
        if number < 1:
            violations.append(
                f"field 'size' must be a positive integer (got '{size}')")
            number = None
    afters = [str(item) for item in (after or [])]
    tasks: list[dict] = []
    if record is not None:
        tasks = _read_tasks(key)[0]
        violations.extend(fronts.validate_commissioned_task(
            {"title": name, "scope": text, "verify": check,
             "size": number if number is not None else size,
             "after": afters},
            tasks))
    if violations:
        return _refuse(violations)
    assert record is not None and number is not None
    role = "owner" if (me is None or me.role == OWNER) else me.role
    who = caller.by_line(me)
    tid = ids.mint("task")
    task = entities.Task(
        id=tid,
        front=key,
        title=name,
        scope=text,
        verify=check,
        size=number,
        after=afters,
        timeout="",
        land_on=record.get("land_on") or "",
        core=False,
        state="ready" if not afters else "waiting",
        units_done=0,
        units_total=number,
        added_by=role,
    )
    store.append_ledger(paths.front_tasks_path(key), task.to_dict(),
                        session_id=who)
    print(f"{tid} added to {key}")
    return 0


def add_task_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="task_verb", required=True)
    add = verbs.add_parser(
        "add", help="Commission a task onto a running front.")
    add.add_argument("front", help="front to append the task to")
    add.add_argument("--title", default=None, help="task title (required)")
    add.add_argument("--scope", default=None,
                     help="task scope carrying WHAT, INPUTS, OUTPUTS and "
                          "OUT OF SCOPE (required)")
    add.add_argument("--verify", default=None,
                     help="verification command (required)")
    add.add_argument("--size", default=None,
                     help="positive integer (required)")
    add.add_argument("--after", nargs="*", default=[],
                     help="titles of tasks this one waits on")
    built = verbs.add_parser("built", help="Mark a task built.")
    built.add_argument("task", help="task id or title")
    built.add_argument("--did-myself", default=None,
                       help="account for the units yourself, in one "
                            "sentence, for work no worker was dispatched to")
    landed = verbs.add_parser("landed", help="Mark a task landed.")
    landed.add_argument("task", help="task id or title")
    landed.add_argument("--head", default=None,
                        help="landed commit (required)")
    reset = verbs.add_parser(
        "reset", help="Put a task back to where it started, with a reason.")
    reset.add_argument("task", help="task id or title")
    reset.add_argument("--reason", default=None,
                       help="why it moved backwards (required)")


@cli.subcommand("task", help="Commission a task, or mark one built, "
                 "landed or reset.")
def _task_entry(args: argparse.Namespace) -> int:
    if args.task_verb == "add":
        return task_add_main(args.front, args.title, args.scope,
                             args.verify, args.size, args.after)
    if args.task_verb == "built":
        return task_built_main(args.task, did_myself=args.did_myself)
    if args.task_verb == "landed":
        return task_landed_main(args.task, head=args.head)
    if args.task_verb == "reset":
        return task_reset_main(args.task, reason=args.reason)
    raise AssertionError(f"unknown task verb {args.task_verb!r}")


_task_entry.add_arguments = add_task_arguments  # type: ignore[attr-defined]


def add_evidence_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--on", default=None,
                     help="task id or title, job id, or front (required)")
    sub.add_argument("--claim", default=None, help="what was found (required)")
    sub.add_argument("--status", default=None,
                     help="CONFIRMED or PLAUSIBLE (required)")
    sub.add_argument("--command", default=None,
                     help="command behind the claim (a CONFIRMED claim needs one)")
    sub.add_argument("--output", default=None,
                     help="command output, or a path to it")


@cli.subcommand("evidence", help="Append an evidence record.")
def _evidence_entry(args: argparse.Namespace) -> int:
    return evidence_main(args.on, args.claim, args.status,
                         command=args.command, output=args.output)


_evidence_entry.add_arguments = add_evidence_arguments  # type: ignore[attr-defined]


def add_finding_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--task", "--on", dest="on", default=None,
                     help="task id or title, or front (--on or --task)")
    sub.add_argument("--class", dest="class_", default=None,
                     help="finding class, one word (required)")
    sub.add_argument("--title", default=None,
                     help="self-contained title (required)")
    sub.add_argument("--detail", default=None, help="detail (required)")
    sub.add_argument("--output-file", dest="output_file", default=None,
                     help="path to evidence, copied under the caller's session")


@cli.subcommand("finding", help="Append a finding record.")
def _finding_entry(args: argparse.Namespace) -> int:
    return finding_main(args.on, args.class_, args.title, args.detail,
                        output_file=args.output_file)


_finding_entry.add_arguments = add_finding_arguments  # type: ignore[attr-defined]
