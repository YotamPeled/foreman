"""Landing policy for a front and a repository.

``policy_for(front, repo)`` is the one answer to what lands how: a v5
front's matching ``[[repository]]`` entry, or the global ``[merge]``
check as a fallback for old-shape fronts and unmatched names. The merge
desk reads this; it does not read ``foreman.toml`` first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .caller import Refusal


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
