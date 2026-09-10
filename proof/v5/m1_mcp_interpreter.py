"""Clause 1.5: a dry-run supervisor summon probes an interpreter that imports foreman."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLAUSE = ("1.5", "mcp interpreter")


def _field(out: str, prefix: str) -> str:
    needle = prefix if prefix.endswith(" ") else prefix + " "
    for line in out.splitlines():
        if line.startswith(needle):
            return line[len(needle):].strip()
    raise Failure(f"no {prefix!r} line in output:\n{out[-2000:]}")


def apply(world) -> None:
    state = Path(os.environ["FOREMAN_STATE"])
    roster = state / "roster.json"
    world["_15_roster"] = roster.read_text(encoding="utf-8") if roster.is_file() else ""
    proc = run_foreman(
        ["register", "--role", "supervisor", "--front", "alpha",
         "--pid", str(os.getpid())])
    if proc.returncode != 0:
        raise Failure(f"BREAK apply register failed: {proc.stderr[-400:]}")


def restore(world) -> None:
    saved = world.pop("_15_roster", None)
    if saved is None:
        return
    path = Path(os.environ["FOREMAN_STATE"]) / "roster.json"
    if saved:
        path.write_text(saved, encoding="utf-8")
    elif path.exists():
        path.write_text('{"sessions": {}}\n', encoding="utf-8")


BREAK = ("register a live supervisor on alpha so the next summon is refused",
         apply, restore)


def run(world) -> str:
    assert_world()
    proc = run_foreman([
        "launch", "supervisor", "alpha",
        "--repo", str(world["repo_a"]),
        "--dry-run",
    ])
    if proc.returncode != 0:
        raise Failure(
            f"supervisor --dry-run exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[-800:]}")
    sid = _field(proc.stdout, "session:")
    state = Path(os.environ["FOREMAN_STATE"])
    path = state / "sessions" / sid / "mcp.json"
    if not path.is_file():
        raise Failure(f"dry-run wrote no mcp.json at {path}")
    server = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["foreman"]
    command = server["command"]
    line = f"mcp: {command} (probed)"
    if line not in proc.stdout.splitlines():
        raise Failure(
            f"dry-run did not print {line!r}:\n{proc.stdout[-1500:]}")
    env = os.environ.copy()
    env.update(server.get("env") or {})
    probed = subprocess.run(
        [command, "-c", "import foreman"],
        capture_output=True, text=True, env=env, timeout=30)
    if probed.returncode != 0:
        raise Failure(
            f"written command {command!r} cannot import foreman: "
            f"{(probed.stderr or probed.stdout)[-400:]}")
    return f"mcp: {command} (probed) imports foreman"
