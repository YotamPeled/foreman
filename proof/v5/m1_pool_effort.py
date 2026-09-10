"""Clause 1.9: launch without --effort uses the pool default; muse at high is refused."""

from __future__ import annotations

import os
from pathlib import Path

CLAUSE = ("1.9", "pool effort")

_SPEC = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/front-v5 -q\n"
)


def _manifest() -> Path:
    return Path(os.environ["FOREMAN_CONFIG"]) / "pools" / "fake" / "manifest.toml"


def _ensure_effort_default() -> str:
    path = _manifest()
    text = path.read_text(encoding="utf-8")
    if "effort_default" not in text:
        text = text.replace(
            'timeout_default = "5m"\n',
            'timeout_default = "5m"\n'
            'effort_default = "xhigh"\n')
        path.write_text(text, encoding="utf-8")
    return "xhigh"


def apply(world) -> None:
    path = _manifest()
    world["_19_manifest"] = path.read_text(encoding="utf-8")
    lines = [line for line in world["_19_manifest"].splitlines(True)
             if not line.startswith("effort_default")]
    path.write_text("".join(lines), encoding="utf-8")


def restore(world) -> None:
    saved = world.pop("_19_manifest", None)
    if saved is None:
        return
    _manifest().write_text(saved, encoding="utf-8")


BREAK = ("strip effort_default from the fake pool so a launch without --effort is refused",
         apply, restore)


def run(world) -> str:
    assert_world()
    if not world.get("_19_ensured"):
        _ensure_effort_default()
        world["_19_ensured"] = True
    default = "xhigh"
    spec = Path(world["specs"]) / "pool-effort.md"
    spec.write_text(_SPEC, encoding="utf-8")
    n = world.get("_19_n", 0) + 1
    world["_19_n"] = n
    root = Path(world["repo_a"]).parent
    defaulted = run_foreman([
        "launch", "muse", "fake", str(spec),
        "--repo", str(world["repo_a"]),
        "--worktree", str(root / f"wt-effort-{n}"),
        "--dry-run",
    ])
    if defaulted.returncode != 0:
        raise Failure(
            f"fake pool dry-run without --effort failed: "
            f"{defaulted.stderr[-800:]}")
    line = f"effort: {default} (pool default)"
    if line not in defaulted.stdout.splitlines():
        raise Failure(
            f"missing {line!r} in:\n{defaulted.stdout[-1500:]}")
    refused = run_foreman([
        "launch", "muse", "muse", str(spec),
        "--repo", str(world["repo_a"]),
        "--worktree", str(root / f"wt-muse-high-{n}"),
        "--effort", "high",
        "--dry-run",
    ])
    if refused.returncode == 0:
        raise Failure("muse --effort high was admitted")
    err = refused.stderr or ""
    if "rul-frzifag" not in err:
        raise Failure(f"muse high refusal did not name rul-frzifag: {err[-800:]}")
    if "got high" not in err:
        raise Failure(f"muse high refusal did not name the effort: {err[-800:]}")
    return (
        f"fake pool default {default}; muse at high refused naming rul-frzifag"
    )
