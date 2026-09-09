"""`foreman merge request|take|land|fail`: the merge queue and the landing.

A front's supervisor hands a built branch over with ``merge request``; the
merge desk consumes the queue first come, first served: ``take`` claims the
oldest unclaimed record, ``land`` rebases it onto the target, runs the
target's check command from ``foreman.toml``, pushes the rebased head to
the target (lease on the sha the rebase was onto) then the branch, marks
the tasks landed, and ``fail`` sends the record back with a reason while
the tasks stay built and the branch is left alone. A target that moved
under the lease is re-queued once (attempt recorded, desk woken) and
failed with the reason on the second refusal.

Gates, per docs/DESIGN.md section 12: ``request`` is the front's own
supervisor (like every progress verb); ``take``, ``land`` and ``fail`` are
the merge desk. The owner is never refused. Every state change appends a
revised copy of the record to ``merges.jsonl`` — no byte already written is
ever edited — and readers fold last-wins with :func:`store.fold_by_id`.

The repository the branches live in is the current working directory: the
desk works in the checkout it was summoned onto, the way a supervisor works
in the repository it is given.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import tomllib

from . import caller, cli, fronts, ids, paths, procs, store, wake
from .caller import MERGE_DESK, OWNER, SUPERVISOR, Refusal
from . import entities

REQUESTED = "requested"
MERGING = "merging"
LANDED = "landed"
FAILED = "failed"

#: Records nobody holds: the legacy lines (empty result) and fresh requests.
UNCLAIMED = ("", REQUESTED)


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


def _merges() -> tuple[list[dict], dict[str, dict]]:
    folded = store.fold_by_id(store.read_ledger(paths.merges_path()))
    return folded, {record["id"]: record for record in folded
                    if isinstance(record.get("id"), str)}


def is_landed(record: dict) -> bool:
    return record.get("landed_at") is not None or \
        record.get("result") == LANDED


def is_failed(record: dict) -> bool:
    return record.get("failed_at") is not None or \
        record.get("result") == FAILED


def _holder_live(holder: str | None) -> bool:
    """Whether the session holding a record is still that session.

    A take held by a dead session is not held at all: the desk is one
    session at a time and a relaunch mints a new id, so refusing the
    successor forever would wedge the queue. The roster stamp plus the
    process identity is the same liveness the launcher checks.
    """
    if not holder:
        return False
    entry = caller.read_roster().get("sessions", {}).get(holder)
    if not isinstance(entry, dict):
        return False
    if entry.get("state") not in ("starting", "running"):
        return False
    pid = entry.get("pid")
    if pid is None:
        return True
    return procs.same_process(pid, entry.get("pid_starttime"))


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _repo_problems(repo: str) -> list[str]:
    """The checkout the branches live in, or why it is not one."""
    if not os.path.isdir(repo):
        return [f"field 'repo' is not a directory ({repo!r}); "
                f"the desk works in the checkout it was summoned onto"]
    proc = _git(repo, "rev-parse", "--git-dir")
    if proc.returncode != 0:
        return [f"field 'repo' is not a git repository ({repo!r})"]
    return []


def _branch_exists(repo: str, branch: str) -> bool:
    return _git(repo, "show-ref", "--verify", "--quiet",
                f"refs/heads/{branch}").returncode == 0


def merge_check_command() -> str | None:
    """The target's check command from ``foreman.toml``, if one is set."""
    path = paths.config_file()
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    table = data.get("merge") if isinstance(data, dict) else None
    if not isinstance(table, dict):
        return None
    check = table.get("check")
    return check.strip() or None if isinstance(check, str) else None


def _resolve_request_task(front: str, ref: str,
                          violations: list[str]) -> dict | None:
    """One requested task: on this front and built, or a named refusal."""
    key = (ref or "").strip()
    if not key:
        violations.append("field '--tasks' names an empty task")
        return None
    for name in _all_fronts():
        record = _read_tasks(name)[1].get(key)
        if record is None:
            continue
        if name != front:
            violations.append(f"task '{key}' is on front '{name}', "
                              f"not '{front}'")
            return None
        if record.get("state") != "built":
            violations.append(
                f"task '{record.get('title')}' is "
                f"'{record.get('state')}', not 'built' "
                f"(only a built task can be requested)")
            return None
        return record
    if front:
        for record in _read_tasks(front)[0]:
            if record.get("title") == key:
                if record.get("state") != "built":
                    violations.append(
                        f"task '{key}' is '{record.get('state')}', "
                        f"not 'built' (only a built task can be requested)")
                    return None
                return record
    violations.append(f"unknown task '{key}' on front '{front}'")
    return None


def merge_request_main(branch: str | None, front: str | None,
                       tasks: list[str] | None,
                       target: str | None,
                       reviews: list[str] | None) -> int:
    verb = "merge request"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    if not front_name:
        violations.append("field '--front' is required for 'merge request'")
    elif fronts.read_front_record(front_name) is None:
        violations.append(f"unknown front '{front_name}'")
    if front_name and fronts.read_front_record(front_name) is not None:
        caller.check_front_supervisor(me, front_name, verb,
                                      violations=violations)
        from .progress import _front_merge_mode
        if _front_merge_mode(front_name) == "self":
            violations.append(
                "the front lands through its own supervisor "
                "('merge = \"self\"'): mark the task landed yourself with "
                "'foreman task landed <task> --head <sha>' rather than "
                "requesting a merge")
    elif me is not None and me.role != OWNER and me.role != SUPERVISOR:
        violations.append(f"role '{me.role}' may not call '{verb}'")
    name = (branch or "").strip()
    if not name:
        violations.append("field 'branch' is required for 'merge request'")
    want = (target or "").strip()
    if not want:
        violations.append("field '--target' is required for 'merge request'")
    wanted = list(tasks or [])
    if not [task for task in wanted if (task or "").strip()]:
        violations.append("field '--tasks' is required for 'merge request' "
                          "(at least one task)")
    repo = os.getcwd()
    repo_bad = _repo_problems(repo)
    if repo_bad and (name or want):
        violations.extend(repo_bad)
    if name and not repo_bad and not _branch_exists(repo, name):
        violations.append(f"field 'branch' does not exist ({name!r})")
    if want and not repo_bad and not _branch_exists(repo, want):
        violations.append(f"field '--target' does not exist ({want!r})")
    resolved: list[dict] = []
    if front_name and fronts.read_front_record(front_name) is not None:
        for ref in wanted:
            record = _resolve_request_task(front_name, ref, violations)
            if record is not None:
                resolved.append(record)
    if violations:
        return _refuse(violations)
    who = caller.by_line(me)
    mid = ids.mint("merge")
    now = store.utcnow_iso()
    store.append_ledger(
        paths.merges_path(),
        entities.Merge(
            id=mid, front=front_name, branch=name,
            tasks=[str(record.get("id")) for record in resolved],
            target=want, review_refs=[str(job).strip() for job in (reviews or [])
                                      if str(job).strip()],
            result=REQUESTED, requested_at=now,
            by=who,
        ).to_dict(),
        session_id=who,
    )
    wake.emit_merge_requested(front_name, mid, name, now)
    print(f"{mid} requested ({name} -> {want})")
    return 0


def _resolve_merge(ref: str, violations: list[str],
                   field: str = "id") -> dict | None:
    key = (ref or "").strip()
    if not key:
        violations.append(f"field '{field}' is required")
        return None
    record = _merges()[1].get(key)
    if record is None:
        violations.append(f"unknown merge '{key}'")
        return None
    return record


def _check_desk(me: caller.Caller | None, verb: str,
                violations: list[str]) -> None:
    caller.check_role(me, verb, MERGE_DESK, violations=violations)


def _land_attempts(record: dict) -> int:
    """How many target-moved refusals this record has already stored."""
    try:
        return int(record.get("land_attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _target_moved_next(record: dict, who: str, reason: str) -> int:
    """Re-queue a target-moved land once; fail it the second time.

    The land still refuses to the caller either way: the desk's turn
    has ended, and the wake (or the fail) is what brings anyone back.
    """
    mid = str(record.get("id"))
    front = str(record.get("front") or "")
    branch = str(record.get("branch") or "")
    now = store.utcnow_iso()
    if _land_attempts(record) == 0:
        store.append_ledger(
            paths.merges_path(),
            dict(record, result=REQUESTED, taken_by=None, taken_at=None,
                 land_attempts=1, fail_reason=reason),
            session_id=who,
        )
        wake.emit_merge_requested(front, mid, branch, now)
        return _refuse([reason])
    store.append_ledger(
        paths.merges_path(),
        dict(record, result=FAILED, fail_reason=reason, failed_at=now),
        session_id=who,
    )
    wake.emit_merge_failed(front, mid, branch, record.get("by"), now)
    return _refuse([reason])


def merge_take_main(ref: str | None) -> int:
    verb = "merge take"
    me, violations = caller.resolve(verb)
    _check_desk(me, verb, violations)
    record = _resolve_merge(ref or "", violations)
    if record is not None:
        if is_landed(record):
            violations.append(f"merge '{record.get('id')}' is already landed")
        elif is_failed(record):
            violations.append(f"merge '{record.get('id')}' is already failed")
        else:
            holder = record.get("taken_by")
            mine = me.session_id if me is not None else None
            if holder and holder != mine and _holder_live(holder):
                violations.append(
                    f"merge '{record.get('id')}' is already taken by "
                    f"'{holder}'")
            if record.get("result") in UNCLAIMED:
                folded, _ = _merges()
                older = [row for row in folded
                         if row.get("id") != record.get("id")
                         and row.get("result") in UNCLAIMED
                         and (row.get("requested_at") or "")
                         < (record.get("requested_at") or "")]
                if older:
                    first = min(older,
                                key=lambda row: row.get("requested_at") or "")
                    violations.append(
                        f"merge '{first.get('id')}' was requested first "
                        f"(first come, first served: take it before "
                        f"'{record.get('id')}')")
    if violations:
        return _refuse(violations)
    assert record is not None
    who = caller.by_line(me)
    if record.get("taken_by") == who:
        print(f"{record.get('id')} already held by this session")
        return 0
    now = store.utcnow_iso()
    store.append_ledger(paths.merges_path(),
                        dict(record, result=MERGING,
                             taken_by=who, taken_at=now),
                        session_id=who)
    print(f"{record.get('id')} taken "
          f"({record.get('branch')} -> {record.get('target')})")
    return 0


def merge_land_main(ref: str | None) -> int:
    verb = "merge land"
    me, violations = caller.resolve(verb)
    _check_desk(me, verb, violations)
    record = _resolve_merge(ref or "", violations)
    if record is not None:
        if is_landed(record):
            violations.append(f"merge '{record.get('id')}' is already landed")
        elif is_failed(record):
            violations.append(f"merge '{record.get('id')}' is already failed")
        elif record.get("result") not in (MERGING,):
            violations.append(
                f"merge '{record.get('id')}' is "
                f"'{record.get('result') or 'unclaimed'}', not taken "
                f"(take it first with 'foreman merge take')")
    repo = os.getcwd()
    violations.extend(_repo_problems(repo))
    check = merge_check_command()
    if check is None:
        violations.append(
            f"no check command in {paths.config_file()} "
            f"('[merge] check'): the desk lands nothing it cannot check")
    if violations:
        return _refuse(violations)
    assert record is not None and check is not None
    who = caller.by_line(me)
    mid = str(record.get("id"))
    branch, target = str(record.get("branch")), str(record.get("target"))
    if not _branch_exists(repo, branch):
        return _refuse([f"field 'branch' does not exist ({branch!r})"])
    # The desk works in a detached worktree of its own, never by checking
    # the branch out in the repository. Every branch it is asked to land
    # was produced by a worker and is still checked out in that worker's
    # worktree, and git refuses to check out a branch twice — so the desk
    # could land nothing at all, and said so as "branch does not exist".
    # Detached, it also never moves the checkout somebody else is working
    # in while it lands.
    with tempfile.TemporaryDirectory(prefix="foreman-merge-") as tmp:
        area = os.path.join(tmp, "landing")
        added = _git(repo, "worktree", "add", "--detach", area, branch)
        if added.returncode != 0:
            tail = (added.stderr.strip() or added.stdout.strip()).strip()
            return _refuse([f"cannot open a landing worktree for "
                            f"'{branch}': {tail}".strip()])
        try:
            # The sha the rebase will sit on, recorded before it runs so
            # the target push can lease against exactly that value.
            base_proc = _git(area, "rev-parse", target)
            if base_proc.returncode != 0:
                return _refuse(
                    [f"field 'target' does not exist ({target!r})"])
            base = base_proc.stdout.strip()
            rebase = _git(area, "rebase", target)
            if rebase.returncode != 0:
                _git(area, "rebase", "--abort")
                tail = (rebase.stderr.strip() or rebase.stdout.strip()).strip()
                return _refuse([
                    f"rebase of '{branch}' onto '{target}' conflicts; "
                    f"the rebase was aborted: {tail}".strip()])
            proc = subprocess.run(check, shell=True, cwd=area,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
            if proc.returncode != 0:
                tail = (proc.stdout or "").strip()[-2000:]
                return _refuse([f"check '{check}' failed on '{branch}' "
                                f"(exit {proc.returncode}): {tail}".strip()])
            head = _git(area, "rev-parse", "HEAD").stdout.strip()
            # Advance the target first, leased to `base`. The rebase put
            # `head` directly on top of `base`, so this is a fast-forward
            # exactly when the remote target is still `base`, and the
            # lease refuses it otherwise. Nothing else is pushed if it
            # refuses — no half-landed branch, no merge commit.
            target_push = _git(
                area, "push",
                f"--force-with-lease=refs/heads/{target}:{base}",
                "origin", f"HEAD:refs/heads/{target}")
            if target_push.returncode != 0:
                remote = _git(area, "ls-remote", "origin",
                              f"refs/heads/{target}").stdout.split()
                found = remote[0] if remote else ""
                tail = (target_push.stderr.strip()
                        or target_push.stdout.strip()).strip()
                reason = (
                    f"push of '{target}' failed: expected {base}, "
                    f"found {found}: {tail}".strip())
                if found and found != base:
                    return _target_moved_next(record, who, reason)
                return _refuse([reason])
            # A rebase rewrites the branch, so the push that follows one is
            # always a force. With-lease, so a branch somebody moved since
            # the request is refused rather than overwritten.
            push = _git(area, "push", "--force-with-lease", "origin",
                        f"HEAD:refs/heads/{branch}")
            if push.returncode != 0:
                tail = (push.stderr.strip() or push.stdout.strip()).strip()
                return _refuse([f"push of '{branch}' failed: {tail}".strip()])
            # And the local branch follows the rebase where it can. Where
            # a worker still has it checked out, git refuses and the remote
            # is the record: the worker's copy is stale either way once it
            # has been rebased, and the landing is what was pushed.
            _git(repo, "branch", "--force", branch, head)
        finally:
            _git(repo, "worktree", "remove", "--force", area)
    # The tasks land one verb at a time, through the same gate the desk
    # itself passed: a task that stopped being built since the request
    # refuses here and the merge stays taken, so a retry resumes it.
    from .progress import task_landed_main
    for task in record.get("tasks") or []:
        if task_landed_main(str(task), head=head) != 0:
            return 1
    now = store.utcnow_iso()
    store.append_ledger(paths.merges_path(),
                        dict(record, result=LANDED, head=head,
                             landed_at=now, land_attempts=0,
                             fail_reason=""),
                        session_id=who)
    wake.emit_merge_landed(str(record.get("front") or ""), mid,
                           str(record.get("branch") or ""),
                           record.get("by"), head, now)
    print(f"{mid} landed {head}")
    return 0


def merge_fail_main(ref: str | None, reason: str | None) -> int:
    verb = "merge fail"
    me, violations = caller.resolve(verb)
    _check_desk(me, verb, violations)
    record = _resolve_merge(ref or "", violations)
    if record is not None:
        if is_landed(record):
            violations.append(f"merge '{record.get('id')}' is already landed")
        elif is_failed(record):
            violations.append(f"merge '{record.get('id')}' is already failed")
    why = (reason or "").strip()
    if not why:
        violations.append("field '--reason' is required for 'merge fail'")
    if violations:
        return _refuse(violations)
    assert record is not None
    who = caller.by_line(me)
    now = store.utcnow_iso()
    store.append_ledger(paths.merges_path(),
                        dict(record, result=FAILED, fail_reason=why,
                             failed_at=now),
                        session_id=who)
    wake.emit_merge_failed(str(record.get("front") or ""),
                           str(record.get("id")),
                           str(record.get("branch") or ""),
                           record.get("by"), now)
    print(f"{record.get('id')} failed: {why}")
    return 0


def add_merge_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="merge_verb", required=True)
    request = verbs.add_parser("request", help="Hand a built branch over.")
    request.add_argument("branch", nargs="?", default=None,
                         help="branch to land (required)")
    request.add_argument("--front", default=None,
                         help="front whose tasks these are (required)")
    request.add_argument("--tasks", nargs="*", default=[],
                         help="built task ids or titles (required)")
    request.add_argument("--target", default=None,
                         help="branch to land onto (required)")
    request.add_argument("--review", dest="reviews", action="append",
                         default=[], help="review job ref; repeatable")
    take = verbs.add_parser("take", help="Claim the oldest unclaimed merge.")
    take.add_argument("id", nargs="?", default=None,
                      help="merge id (required)")
    land = verbs.add_parser("land", help="Rebase, check, push and land.")
    land.add_argument("id", nargs="?", default=None,
                      help="merge id (required)")
    fail = verbs.add_parser("fail", help="Fail a merge with a reason.")
    fail.add_argument("id", nargs="?", default=None,
                      help="merge id (required)")
    fail.add_argument("--reason", default=None,
                      help="why it cannot land (required)")


@cli.subcommand("merge", help="Request, take, land or fail a merge.")
def _merge_entry(args: argparse.Namespace) -> int:
    if args.merge_verb == "request":
        return merge_request_main(args.branch, args.front,
                                  args.tasks, args.target,
                                  args.reviews)
    if args.merge_verb == "take":
        return merge_take_main(args.id)
    if args.merge_verb == "land":
        return merge_land_main(args.id)
    if args.merge_verb == "fail":
        return merge_fail_main(args.id, reason=args.reason)
    raise AssertionError(f"unknown merge verb {args.merge_verb!r}")


_merge_entry.add_arguments = add_merge_arguments  # type: ignore[attr-defined]
