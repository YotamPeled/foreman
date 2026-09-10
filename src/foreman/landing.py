"""Landing policy for a front and a repository.

``policy_for(front, repo)`` is the one answer to what lands how: a v5
front's matching ``[[repository]]`` entry, or the global ``[merge]``
check as a fallback for old-shape fronts and unmatched names. The merge
desk reads this; it does not read ``foreman.toml`` first.

``run`` lands a job branch onto the front's work branch under a
per-repository lock: fetch, refuse an empty range, rebase in a detached
worktree, run the policy's check, push with a lease. A ``front-landing``
item does the same from the work branch onto the target; ``land = pr``
pushes work and opens a pull request. A ``rebase`` item rebases work
onto a moved target and pushes work.
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

#: Script items the queue tick runs without a slot. A job landing is
#: kind ``job`` with ``role = script``; these two are their own kinds.
SCRIPT_KINDS = ("front-landing", "rebase")


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
    pr_url: str = ""


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


def _landing_names(front: str, item: dict) -> tuple[str, str]:
    """Branch and onto-name a failed landing title quotes."""
    kind = str(item.get("kind") or "").strip()
    if kind == "front-landing":
        return (str(item.get("lands") or "").strip(),
                str(item.get("target") or "").strip())
    if kind == "rebase":
        return (str(item.get("lands") or "").strip(),
                str(item.get("onto") or "").strip())
    lands_id = str(item.get("lands") or "").strip()
    branch = ""
    if lands_id:
        job = _verified_job(front, {"id": lands_id})
        if job is not None and isinstance(job.get("branch"), str):
            branch = (job.get("branch") or "").strip()
        if not branch:
            branch = f"job/{front}-{lands_id}"
    target = ""
    repo = str(item.get("repo") or "").strip()
    if repo:
        try:
            target = policy_for(front, repo).work
        except Refusal:
            target = ""
    return branch, target


def _fail_detail(result: LandingResult) -> str:
    dropped = ", ".join(result.dropped) if result.dropped else ""
    exit_s = "" if result.exit is None else str(result.exit)
    seconds_s = "" if result.seconds is None else str(result.seconds)
    return "\n".join([
        f"command: {result.command}",
        f"exit: {exit_s}",
        f"seconds: {seconds_s}",
        f"dropped: {dropped}",
        result.fail_reason,
    ])


def _file_landing_finding(front: str, item: dict, by: str,
                          result: LandingResult) -> str:
    """Append one landing finding on ``front``. Returns the finding id."""
    from . import entities, ids, paths, store
    from . import progress as progress_mod

    branch, target = _landing_names(front, item)
    first = ""
    for line in (result.fail_reason or "").splitlines():
        first = line.strip()
        if first:
            break
    title = f"landing of {branch} onto {target} failed: {first}"
    fid = ids.mint("finding")
    copied = ""
    src = (result.output_file or "").strip()
    if src and Path(src).is_file():
        dest = paths.front_findings_dir(front) / f"{fid}-{Path(src).name}"
        copied = progress_mod._store_output_file(src, dest)
    store.append_ledger(
        paths.front_findings_path(front),
        entities.Finding(
            id=fid, on=front, class_="landing", title=title,
            detail=_fail_detail(result), evidence_ref=copied,
        ).to_dict(),
        session_id=by,
    )
    return fid


def _record_item(front: str, item: dict, by: str, result: LandingResult) -> None:
    from . import progress as progress_mod

    changes: dict[str, object] = {}
    if result.ok:
        changes["state"] = "landed"
        changes["landed_sha"] = result.head
        changes["fail_reason"] = ""
        changes["finding"] = ""
    else:
        changes["state"] = "failed"
        changes["fail_reason"] = result.fail_reason
        changes["finding"] = _file_landing_finding(front, item, by, result)
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
    if result.pr_url:
        changes["pr_url"] = result.pr_url
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


def create_pr(*, base: str, head: str, title: str, body: str,
              cwd: str) -> subprocess.CompletedProcess[str]:
    """Open a pull request. Tests replace this function."""
    return subprocess.run(
        ["gh", "pr", "create",
         "--base", base, "--head", head,
         "--title", title, "--body", body],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)


def _pr_url_from_output(output: str) -> str:
    for line in reversed((output or "").splitlines()):
        text = line.strip()
        if text.startswith("http://") or text.startswith("https://"):
            return text
    stripped = (output or "").strip()
    if not stripped:
        return ""
    return stripped.splitlines()[-1].strip()


def _origin_url(policy: Policy, fallback: str) -> str:
    url = (policy.url or "").strip()
    return url or fallback


def _revise_front(front_name: str, record: dict, who: str,
                  **changes: object) -> dict:
    from . import entities, store
    from . import paths as paths_mod

    updated = dict(record)
    updated.update(changes)
    line = entities.Front.from_dict(updated).to_dict()
    if "monitors" in record:
        line["monitors"] = record["monitors"]
    store.append_ledger(
        paths_mod.front_record_path(front_name), line, session_id=who)
    return line


def _set_front_base(front_name: str, record: dict | None, who: str,
                    *, repo_name: str, base_sha: str,
                    behind: str | None = None) -> None:
    if record is None:
        return
    repos = []
    for entry in record.get("repositories") or []:
        if not isinstance(entry, dict):
            repos.append(entry)
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if repo_name and name != repo_name and url != repo_name:
            repos.append(entry)
            continue
        repos.append(dict(entry, base_sha=base_sha))
    changes: dict[str, object] = {
        "repositories": repos, "base_sha": base_sha,
    }
    if behind is not None:
        changes["behind"] = behind
    _revise_front(front_name, record, who, **changes)


def _setup_land_clone(url: str, area: str) -> str:
    """Clone ``url`` into ``area``. Empty string on success, else a reason."""
    from . import paths as paths_mod

    paths_mod.remove_scratch(area)
    cloned = subprocess.run(
        ["git", "clone", "-q", url, area],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if cloned.returncode != 0:
        tail = (cloned.stderr.strip() or cloned.stdout.strip()).strip()
        return f"cannot clone '{url}': {tail}".strip()
    _git(area, "config", "user.email", "foreman@invalid")
    _git(area, "config", "user.name", "foreman")
    fetched = _git(area, "fetch", "-q", "origin")
    if fetched.returncode != 0:
        tail = (fetched.stderr.strip() or fetched.stdout.strip()).strip()
        return f"git fetch origin failed: {tail}".strip()
    return ""


def _checkout_work(area: str, work: str) -> str:
    checked = _git(area, "checkout", "-q", "-B", work, f"origin/{work}")
    if checked.returncode != 0:
        tail = (checked.stderr.strip() or checked.stdout.strip()).strip()
        return f"work branch '{work}' does not resolve on origin: {tail}".strip()
    return ""


def _remote_sha(area: str, branch: str) -> str:
    proc = _git(area, "rev-parse", f"origin/{branch}")
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _run_check(area: str, command: str, item_id: str
               ) -> tuple[int, float, str, str]:
    from . import paths as paths_mod

    child_env = _check_env(command)
    started = time.perf_counter()
    proc = subprocess.run(
        command, shell=True, cwd=area,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=child_env)
    elapsed = time.perf_counter() - started
    output = proc.stdout or ""
    log_path = paths_mod.landing_check_log_path(item_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    exit_code = proc.returncode if proc.returncode is not None else 1
    return exit_code, elapsed, str(log_path), output


def _trailer_commit(area: str, front: str, target: str,
                    trailers: list[str]) -> str:
    if not trailers:
        return ""
    cmd = [
        "-c", "user.email=foreman@invalid",
        "-c", "user.name=foreman",
        "commit", "--allow-empty",
        "-m", f"land {front} onto {target}",
    ]
    for trailer in trailers:
        cmd.extend(["--trailer", trailer])
    committed = _git(area, *cmd)
    if committed.returncode != 0:
        tail = (committed.stderr.strip() or committed.stdout.strip()).strip()
        return f"trailer commit failed: {tail}".strip()
    return ""


def _rebase_onto(area: str, onto: str, label: str) -> str:
    rebase = _git(area, "rebase", onto)
    if rebase.returncode == 0:
        return ""
    conflict = _first_conflict_file(area)
    _git(area, "rebase", "--abort")
    if conflict:
        return conflict
    tail = (rebase.stderr.strip() or rebase.stdout.strip()).strip()
    return f"rebase of '{label}' onto '{onto}' failed: {tail}".strip()


def _run_front_landing_locked(front: str, item: dict, *, by: str,
                              front_record: dict | None,
                              repo: str) -> LandingResult:
    from . import paths as paths_mod

    try:
        policy = policy_for(
            front, str(item.get("repo") or repo))
    except Refusal as exc:
        return _fail("; ".join(exc.violations))
    work = policy.work
    target = str(item.get("target") or policy.target or "").strip()
    if not work:
        return _fail("repository names no work branch")
    if not target:
        return _fail("repository names no target branch")
    url = _origin_url(policy, repo)
    item_id = str(item.get("id") or "land")
    area = str(paths_mod.scratch_worktree_dir("front-land", item_id))
    try:
        err = _setup_land_clone(url, area)
        if err:
            return _fail(err)
        err = _checkout_work(area, work)
        if err:
            return _fail(err)
        base_sha = _remote_sha(area, target)
        if not base_sha:
            return _fail(f"target branch '{target}' does not resolve on origin")
        work_sha = _remote_sha(area, work)
        before = _rev_list(area, f"{base_sha}..HEAD")
        if not before:
            return _fail(
                f"{work} adds nothing to {target}@ {base_sha}",
                base_sha=base_sha)
        err = _rebase_onto(area, base_sha, work)
        if err:
            return _fail(err, base_sha=base_sha)
        err = _trailer_commit(area, front, target, policy.trailers)
        if err:
            return _fail(err, base_sha=base_sha)
        after = _rev_list(area, f"{base_sha}..HEAD")
        after_set = set(after)
        dropped = [sha for sha in before if sha not in after_set]
        command = policy.check
        exit_code, elapsed, output_file, _output = _run_check(
            area, command, item_id)
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
        if policy.land == "pr":
            lease = work_sha or work
            push = _git(
                area, "push",
                f"--force-with-lease=refs/heads/{work}:{lease}",
                "origin", f"HEAD:refs/heads/{work}")
            if push.returncode != 0:
                tail = (push.stderr.strip() or push.stdout.strip()).strip()
                return _fail(
                    f"push of '{work}' failed: {tail}".strip(), **fields)
            title = ""
            if front_record is not None:
                title = str(front_record.get("goal") or "").strip()
            if not title:
                title = front
            created = create_pr(
                base=target, head=work, title=title,
                body=policy.pr_body, cwd=area)
            if created.returncode != 0:
                tail = (created.stdout or "").strip()
                return _fail(
                    f"gh pr create failed: {tail}".strip(), **fields)
            fields["pr_url"] = _pr_url_from_output(created.stdout or "")
            return LandingResult(ok=True, **fields)
        push = _git(
            area, "push",
            f"--force-with-lease=refs/heads/{target}:{base_sha}",
            "origin", f"HEAD:refs/heads/{target}")
        if push.returncode != 0:
            tail = (push.stderr.strip() or push.stdout.strip()).strip()
            return _fail(
                f"push of '{target}' failed: {tail}".strip(), **fields)
        result = LandingResult(ok=True, **fields)
        _set_front_base(
            front, front_record, by,
            repo_name=str(item.get("repo") or ""),
            base_sha=head)
        return result
    finally:
        paths_mod.remove_scratch(area)


def _run_rebase_locked(front: str, item: dict, *, by: str,
                       front_record: dict | None,
                       repo: str) -> LandingResult:
    from . import paths as paths_mod

    try:
        policy = policy_for(
            front, str(item.get("repo") or repo))
    except Refusal as exc:
        return _fail("; ".join(exc.violations))
    work = policy.work
    if not work:
        return _fail("repository names no work branch")
    onto = str(item.get("onto") or "").strip()
    if not onto:
        return _fail("rebase item names no onto sha")
    url = _origin_url(policy, repo)
    item_id = str(item.get("id") or "rebase")
    area = str(paths_mod.scratch_worktree_dir("rebase", item_id))
    try:
        err = _setup_land_clone(url, area)
        if err:
            return _fail(err)
        err = _checkout_work(area, work)
        if err:
            return _fail(err)
        work_sha = _remote_sha(area, work)
        before = _rev_list(area, f"{onto}..HEAD")
        err = _rebase_onto(area, onto, work)
        if err:
            return _fail(err, base_sha=onto)
        after = _rev_list(area, f"{onto}..HEAD")
        after_set = set(after)
        dropped = [sha for sha in before if sha not in after_set]
        command = policy.check
        exit_code, elapsed, output_file, _output = _run_check(
            area, command, item_id)
        head_proc = _git(area, "rev-parse", "HEAD")
        head = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
        fields = dict(
            command=command, exit=exit_code, seconds=elapsed,
            output_file=output_file, head=head, base_sha=onto,
            dropped=dropped)
        if exit_code != 0:
            return _fail(
                f"check '{command}' failed (exit {exit_code})",
                **fields)
        lease = work_sha or work
        push = _git(
            area, "push",
            f"--force-with-lease=refs/heads/{work}:{lease}",
            "origin", f"HEAD:refs/heads/{work}")
        if push.returncode != 0:
            tail = (push.stderr.strip() or push.stdout.strip()).strip()
            return _fail(f"push of '{work}' failed: {tail}".strip(), **fields)
        _set_front_base(
            front, front_record, by,
            repo_name=str(item.get("repo") or ""),
            base_sha=onto, behind="")
        return LandingResult(ok=True, **fields)
    finally:
        paths_mod.remove_scratch(area)


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

    A ``front-landing`` item lands work onto the target; a ``rebase``
    item rebases work onto a moved target. Holds the per-repository
    lock for the whole attempt. On failure the item is ``failed`` with
    ``fail_reason`` and the built node is left alone. The scratch
    worktree is gone on every path.
    """
    from . import fronts
    from . import node as node_mod

    front_name = (front or "").strip()
    front_record = fronts.read_front_record(front_name) if front_name else None
    kind = str(item.get("kind") or "").strip()
    lands_id = str(item.get("lands") or "").strip()
    built: dict | None = None
    if kind in SCRIPT_KINDS:
        built = item
    elif front_record is not None and lands_id:
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
        if kind == "front-landing":
            result = _run_front_landing_locked(
                front_name, item, by=by,
                front_record=front_record, repo=repo)
        elif kind == "rebase":
            result = _run_rebase_locked(
                front_name, item, by=by,
                front_record=front_record, repo=repo)
        else:
            result = _run_locked(
                front_name, item, by=by,
                front_record=front_record, built=built)
    _record_item(front_name, item, by, result)
    if result.ok and kind not in SCRIPT_KINDS:
        _mark_built_landed(front_name, built, by, result.head)
    return result

