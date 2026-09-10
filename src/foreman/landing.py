"""Landing policy for a front and a repository.

``policy_for(front, repo)`` is the one answer to what lands how: a v5
front's matching ``[[repository]]`` entry, or the global ``[merge]``
check as a fallback for old-shape fronts and unmatched names. The merge
desk reads this; it does not read ``foreman.toml`` first.

``run`` lands a job branch onto the front's work branch under a
per-repository lock: fetch, refuse an empty range, rebase in a detached
worktree, run the policy's check, push with a lease.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .caller import Refusal, SESSION_ENV


@dataclass(frozen=True)
class Policy:
    """How one repository on one front lands.

    ``source`` is ``repository <name>`` when a v5 entry filled the
    record, or ``[merge] fallback`` when the global check did.
    """

    check: str
    land: str
    trailers: list[str]
    pr_body: str
    script: str
    base: str
    work: str
    target: str
    url: str
    source: str


def _pathish(value: str) -> bool:
    return value.startswith("/") or value.startswith(".") or os.path.sep in value


def _repo_matches(entry: dict, repo: str) -> bool:
    """True when ``repo`` is this entry's name or url."""
    want = (repo or "").strip()
    if not want:
        return False
    name = str(entry.get("name") or "").strip()
    url = str(entry.get("url") or "").strip()
    if name == want or url == want:
        return True
    if _pathish(url) and _pathish(want):
        return os.path.realpath(url) == os.path.realpath(want)
    return False


def _as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _policy_from_entry(entry: dict) -> Policy:
    name = _as_str(entry.get("name"))
    land = entry.get("land")
    if land not in ("push", "pr"):
        land = "push"
    raw = entry.get("trailers")
    trailers = [item for item in raw if isinstance(item, str)] if isinstance(
        raw, list) else []
    return Policy(
        check=_as_str(entry.get("check")),
        land=land,
        trailers=list(trailers),
        pr_body=entry["pr_body"] if isinstance(entry.get("pr_body"), str)
        else "",
        script=_as_str(entry.get("script")),
        base=_as_str(entry.get("base")),
        work=_as_str(entry.get("work")),
        target=_as_str(entry.get("target")),
        url=_as_str(entry.get("url")),
        source=f"repository {name}" if name else "repository",
    )


def _fallback(check: str) -> Policy:
    return Policy(
        check=check,
        land="push",
        trailers=[],
        pr_body="",
        script="",
        base="",
        work="",
        target="",
        url="",
        source="[merge] fallback",
    )


def policy_for(front: str, repo: str) -> Policy:
    """The landing policy for ``front`` and ``repo``.

    A v5 front's repository entry whose name or url matches ``repo``
    fills the record. A front with no such entry, or an old-shape front,
    falls back to :func:`foreman.merge.merge_check_command` with
    ``land = push`` and ``source = "[merge] fallback"``. Raises
    :class:`Refusal` naming both when neither has a check.
    """
    from . import fronts, merge

    record = fronts.read_front_record(front) if (front or "").strip() else None
    if record is not None and record.get("shape") == "v5":
        repos = record.get("repositories") or []
        if isinstance(repos, list):
            for entry in repos:
                if not isinstance(entry, dict) or not _repo_matches(entry, repo):
                    continue
                policy = _policy_from_entry(entry)
                if policy.check:
                    return policy
                break
    check = merge.merge_check_command(repo)
    if check:
        return _fallback(check)
    raise Refusal([
        f"no check for front '{front}' repository '{repo}'",
    ])


def format_policy(policy: Policy) -> str:
    """One field per line, for ``foreman front policy``."""
    return "\n".join([
        f"check: {policy.check}",
        f"land: {policy.land}",
        f"trailers: {', '.join(policy.trailers)}",
        f"pr_body: {policy.pr_body}",
        f"script: {policy.script}",
        f"base: {policy.base}",
        f"work: {policy.work}",
        f"target: {policy.target}",
        f"url: {policy.url}",
        f"source: {policy.source}",
    ])


@dataclass
class LandingResult:
    """What ``run`` recorded on the landing item."""

    ok: bool
    head: str = ""
    command: str = ""
    exit: int | None = None
    seconds: float | None = None
    output_file: str = ""
    base_sha: str = ""
    dropped: list[str] = field(default_factory=list)
    fail_reason: str = ""


def lock_path_for(repo: str) -> Path:
    """The per-repository lock file under the state directory."""
    from . import paths

    resolved = os.path.realpath(repo) if repo else repo
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    path = paths.landing_lock_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _repo_lock(repo: str) -> Iterator[None]:
    path = lock_path_for(repo)
    with open(path, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    from . import merge

    return merge._git(repo, *args)


def _rev_list(repo: str, rev_range: str) -> list[str]:
    proc = _git(repo, "rev-list", "--reverse", rev_range)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _first_conflict_file(area: str) -> str:
    proc = _git(area, "diff", "--name-only", "--diff-filter=U")
    for line in proc.stdout.splitlines():
        name = line.strip()
        if name:
            return name
    listed = _git(area, "ls-files", "-u")
    for line in listed.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
    return ""


def _check_env(command: str) -> dict[str, str]:
    """FOREMAN variables unset; PYTHONPATH=src only for a python check."""
    from . import paths

    child = os.environ.copy()
    for name in (SESSION_ENV, paths.STATE_ENV, paths.CONFIG_ENV):
        child.pop(name, None)
    if command.strip().startswith("python"):
        child["PYTHONPATH"] = "src"
    return child


def _verified_job(front: str, node: dict) -> dict | None:
    from . import paths, store
    from . import progress as progress_mod

    job_ids = progress_mod._node_job_ids(front, node)
    if not job_ids:
        return None
    try:
        jobs = store.fold_by_id(store.read_ledger(paths.front_jobs_path(front)))
    except OSError:
        return None
    by_id = {row["id"]: row for row in jobs if isinstance(row.get("id"), str)}
    for jid in job_ids:
        record = by_id.get(jid)
        if record is not None and record.get("state") == "verified":
            return record
    return by_id.get(job_ids[0])


def _landing_repo(front: str, front_record: dict | None, built: dict,
                  job: dict | None) -> str:
    from . import launch as launch_mod
    from . import progress as progress_mod

    if job is not None:
        repo = progress_mod._job_repository(job)
        if repo and os.path.isdir(repo):
            return repo
    return launch_mod._queued_repo_path(front_record, built)


def _record_item(front: str, item: dict, by: str, result: LandingResult) -> None:
    from . import progress as progress_mod

    changes: dict[str, object] = {}
    if result.ok:
        changes["state"] = "landed"
        changes["landed_sha"] = result.head
        changes["fail_reason"] = ""
    else:
        changes["state"] = "failed"
        changes["fail_reason"] = result.fail_reason
    if result.command:
        changes["command"] = result.command
    if result.exit is not None:
        changes["exit"] = result.exit
    if result.seconds is not None:
        changes["seconds"] = result.seconds
    if result.output_file:
        changes["output_file"] = result.output_file
    if result.head:
        changes["head"] = result.head
    if result.base_sha:
        changes["base_sha"] = result.base_sha
    if result.dropped:
        changes["dropped"] = list(result.dropped)
    progress_mod._write_node_revise(
        front, item, by, "landing", **changes)


def _mark_built_landed(front: str, built: dict, by: str, head: str) -> None:
    from . import progress as progress_mod

    progress_mod._write_node_revise(
        front, built, by, "landed",
        state="landed", landed_sha=head, waits="")


def _fail(reason: str, **fields: object) -> LandingResult:
    result = LandingResult(ok=False, fail_reason=reason)
    for key, value in fields.items():
        setattr(result, key, value)
    return result


def _run_locked(front: str, item: dict, *, by: str,
                front_record: dict | None, built: dict
                ) -> LandingResult:
    from . import paths

    job = _verified_job(front, built)
    repo = _landing_repo(front, front_record, built, job)
    try:
        policy = policy_for(front, str(built.get("repo") or item.get("repo") or repo))
    except Refusal as exc:
        return _fail("; ".join(exc.violations))
    work = policy.work
    if not work:
        return _fail("repository names no work branch")
    branch = ""
    if job is not None and isinstance(job.get("branch"), str):
        branch = job.get("branch") or ""
    if not branch:
        branch = f"job/{front}-{built.get('id')}"
    item_id = str(item.get("id") or "land")
    fetch = _git(repo, "fetch", "origin", work)
    if fetch.returncode != 0:
        tail = (fetch.stderr.strip() or fetch.stdout.strip()).strip()
        return _fail(f"git fetch origin {work} failed: {tail}".strip())
    remote_ref = f"origin/{work}"
    base_proc = _git(repo, "rev-parse", remote_ref)
    if base_proc.returncode != 0:
        base_proc = _git(repo, "rev-parse", f"refs/heads/{work}")
    if base_proc.returncode != 0:
        return _fail(f"work branch '{work}' does not resolve on origin")
    base_sha = base_proc.stdout.strip()
    before = _rev_list(repo, f"{base_sha}..{branch}")
    if not before:
        return _fail(
            f"{branch} adds nothing to {work}@ {base_sha}",
            base_sha=base_sha)
    area = str(paths.scratch_worktree_dir("land", item_id))
    added = _git(repo, "worktree", "add", "--detach", area, branch)
    if added.returncode != 0:
        tail = (added.stderr.strip() or added.stdout.strip()).strip()
        paths.remove_scratch(area)
        return _fail(
            f"cannot open a landing worktree for '{branch}': {tail}".strip(),
            base_sha=base_sha)
    try:
        rebase = _git(area, "rebase", base_sha)
        if rebase.returncode != 0:
            conflict = _first_conflict_file(area)
            _git(area, "rebase", "--abort")
            if conflict:
                return _fail(conflict, base_sha=base_sha)
            tail = (rebase.stderr.strip() or rebase.stdout.strip()).strip()
            return _fail(
                f"rebase of '{branch}' onto '{work}' failed: {tail}".strip(),
                base_sha=base_sha)
        after = _rev_list(area, f"{base_sha}..HEAD")
        after_set = set(after)
        dropped = [sha for sha in before if sha not in after_set]
        command = policy.check
        child_env = _check_env(command)
        started = time.perf_counter()
        proc = subprocess.run(
            command, shell=True, cwd=area,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=child_env)
        elapsed = time.perf_counter() - started
        output = proc.stdout or ""
        log_path = paths.landing_check_log_path(item_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        output_file = str(log_path)
        exit_code = proc.returncode if proc.returncode is not None else 1
        head_proc = _git(area, "rev-parse", "HEAD")
        head = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
        fields = dict(
            command=command, exit=exit_code, seconds=elapsed,
            output_file=output_file, head=head, base_sha=base_sha,
            dropped=dropped)
        if exit_code != 0:
            return _fail(
                f"check '{command}' failed (exit {exit_code})",
                **fields)
        push = _git(
            area, "push",
            f"--force-with-lease=refs/heads/{work}:{base_sha}",
            "origin", f"HEAD:refs/heads/{work}")
        if push.returncode != 0:
            tail = (push.stderr.strip() or push.stdout.strip()).strip()
            return _fail(f"push of '{work}' failed: {tail}".strip(), **fields)
        return LandingResult(ok=True, **fields)
    finally:
        _git(repo, "worktree", "remove", "--force", area)
        paths.remove_scratch(area)


def run(front: str, item: dict, *, by: str) -> LandingResult:
    """Land the item's job branch onto the front's work branch.

    Holds the per-repository lock for the whole attempt. On failure the
    item is ``failed`` with ``fail_reason`` and the built node is left
    alone. The scratch worktree is gone on every path.
    """
    from . import fronts
    from . import node as node_mod

    front_name = (front or "").strip()
    front_record = fronts.read_front_record(front_name) if front_name else None
    lands_id = str(item.get("lands") or "").strip()
    built: dict | None = None
    if front_record is not None and lands_id:
        _folded, by_id = node_mod._read_nodes(front_name)
        built = by_id.get(lands_id)
        if built is None:
            built = item
    if built is None:
        result = _fail("landing item names no built node")
        if front_name:
            _record_item(front_name, item, by, result)
        return result
    repo = _landing_repo(front_name, front_record, built,
                         _verified_job(front_name, built))
    with _repo_lock(repo):
        result = _run_locked(
            front_name, item, by=by,
            front_record=front_record, built=built)
    _record_item(front_name, item, by, result)
    if result.ok:
        _mark_built_landed(front_name, built, by, result.head)
    return result

