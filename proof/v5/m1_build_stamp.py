"""Clause 1.7: a ledger line appended by a verb carries this checkout's HEAD."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLAUSE = ("1.7", "build stamp")

REPO = Path(__file__).resolve().parents[2]


def _head() -> str:
    proc = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"rev-parse HEAD failed: {proc.stderr[-400:]}")
    return proc.stdout.strip()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def apply(world) -> None:
    path = Path(os.environ["FOREMAN_STATE"]) / "fronts" / "alpha" / "evidence.jsonl"
    world["_17_evidence"] = path.read_text(encoding="utf-8") if path.is_file() else ""
    world["_17_head"] = True


def restore(world) -> None:
    saved = world.pop("_17_evidence", None)
    world.pop("_17_head", None)
    if saved is None:
        return
    path = Path(os.environ["FOREMAN_STATE"]) / "fronts" / "alpha" / "evidence.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(saved, encoding="utf-8")


BREAK = ("compare the stamped commit against a dummy sha", apply, restore)


def run(world) -> str:
    assert_world()
    head = "0" * 40 if world.get("_17_head") else _head()
    proc = run_foreman([
        "evidence", "--on", "alpha", "--claim", "seen it",
        "--status", "PLAUSIBLE",
    ])
    if proc.returncode != 0:
        raise Failure(f"evidence failed: {proc.stderr[-800:]}")
    if f"build {head}" not in proc.stdout:
        raise Failure(
            f"evidence did not print build {head}: {proc.stdout[-800:]}")
    path = Path(os.environ["FOREMAN_STATE"]) / "fronts" / "alpha" / "evidence.jsonl"
    rows = _read_jsonl(path)
    if not rows:
        raise Failure("no evidence line was appended")
    build = rows[-1].get("build") or {}
    if build.get("commit") != head:
        raise Failure(
            f"build.commit is {build.get('commit')!r}, not {head}")
    return f"evidence line build.commit={head[:12]}"
