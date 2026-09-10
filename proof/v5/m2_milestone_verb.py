"""Clause 2.1: milestone add, split, merge and list in the isolated world."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLAUSE = ("2.1", "milestone_verb")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "milestone.py"
_NEEDLE = (
    "    if not reason_text:\n"
    "        violations.append(\"field '--reason' is required\")\n"
)
_PATCH = (
    "    if False and not reason_text:\n"
    "        violations.append(\"field '--reason' is required\")\n"
)

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
]
supervisor = "grok-4.6:high"
team = [
  "grok-4.6:high:1:supervisor",
  "grok-4.6:high:1:builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "v5"
target = "main"
check = "python -m pytest tests -q"
'''


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True)


def _field(out: str, prefix: str) -> str:
    needle = prefix if prefix.endswith(" ") else prefix + " "
    for line in out.splitlines():
        if line.startswith(needle):
            return line[len(needle):].strip()
    raise Failure(f"no {prefix!r} line in output:\n{out[-2000:]}")


def _drop_pyc(path: Path) -> None:
    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return
    stem = path.stem
    for item in cache.iterdir():
        if item.name.startswith(stem + "."):
            item.unlink(missing_ok=True)


def _bare(world) -> Path:
    stored = world.get("_21_bare")
    if stored is not None:
        return Path(stored)
    root = Path(world["repo_a"]).parent / "m2-milestone"
    src = root / "src"
    src.mkdir(parents=True)
    for argv in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "proof@example.invalid"),
                 ("config", "user.name", "proof"),
                 ("commit", "-q", "--allow-empty", "-m", "init")):
        proc = _git(src, *argv)
        if proc.returncode != 0:
            raise Failure(f"git {' '.join(argv)} failed: {proc.stderr[-400:]}")
    bare = root / "remote.git"
    proc = subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(bare)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"bare clone failed: {proc.stderr[-400:]}")
    world["_21_bare"] = str(bare)
    return bare


def _add_front(world, name: str, url: str) -> None:
    directory = Path(world["specs"]) / f"m2-mv-{name}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(name=name, url=url), encoding="utf-8")
    added = run_foreman(["front", "add", str(directory)])
    if added.returncode != 0:
        raise Failure(f"front add {name} failed: {added.stderr[-800:]}")


def _register(front: str) -> str:
    proc = run_foreman(
        ["register", "--role", "supervisor", "--front", front,
         "--pid", str(os.getpid())])
    if proc.returncode != 0:
        raise Failure(f"register supervisor failed: {proc.stderr[-800:]}")
    return _field(proc.stdout, "session:")


def _front(world) -> tuple[str, str]:
    bare = _bare(world)
    n = world.get("_21_n", 0) + 1
    world["_21_n"] = n
    name = f"mv{n}"
    _add_front(world, name, str(bare))
    return name, _register(name)


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_21_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_21_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "drop the '--reason' required check in milestone_add_main",
    apply, restore,
)


def _add_argv(front: str, title: str, *, reason: str | None = "need this") -> list[str]:
    argv = [
        "milestone", "add", front,
        "--title", title,
        "--done-when", f"done when {title}",
        "--verify", "python -m pytest tests -q",
        "--break", f"drop {title}",
    ]
    if reason is not None:
        argv.extend(["--reason", reason])
    return argv


def run(world) -> str:
    assert_world()
    name, sid = _front(world)
    added = run_foreman(
        _add_argv(name, "Front inputs"), session=sid)
    if added.returncode != 0:
        raise Failure(f"milestone add failed: {added.stderr[-800:]}")
    source = added.stdout.strip()
    if not source.startswith("mil-"):
        raise Failure(f"milestone add printed {source!r}, not a mil- id")
    refused = run_foreman(
        _add_argv(name, "No reason", reason=None), session=sid)
    if refused.returncode == 0:
        raise Failure("milestone add without --reason was admitted")
    err = refused.stderr or ""
    if "reason" not in err:
        raise Failure(f"refusal did not name reason: {err[-800:]}")
    split = run_foreman([
        "milestone", "split", name, source, "--reason", "split for size",
        "--into", "Part A",
        "--part", "Part A|done when Part A|python -m pytest tests -q|break A",
        "--into", "Part B",
        "--part", "Part B|done when Part B|python -m pytest tests -q|break B",
    ], session=sid)
    if split.returncode != 0:
        raise Failure(f"milestone split failed: {split.stderr[-800:]}")
    parts = split.stdout.strip().split()
    if len(parts) != 2 or not all(item.startswith("mil-") for item in parts):
        raise Failure(f"split printed {split.stdout.strip()!r}, not two mil- ids")
    listed = run_foreman(["milestone", "list", name, "--json"], session=sid)
    if listed.returncode != 0:
        raise Failure(f"milestone list after split failed: {listed.stderr[-800:]}")
    payload = json.loads(listed.stdout)
    live = payload.get("milestones") or []
    live_ids = [item.get("id") for item in live]
    if live_ids != parts:
        raise Failure(
            f"after split live ids {live_ids!r}, not the two parts {parts!r}")
    if source in live_ids:
        raise Failure("split left the source id live")
    titles = [item.get("title") for item in live]
    if titles != ["Part A", "Part B"]:
        raise Failure(f"list after split not in order: {titles!r}")
    merged = run_foreman([
        "milestone", "merge", name, parts[0], parts[1],
        "--title", "Parts together",
        "--done-when", "both parts are one again",
        "--verify", "python -m pytest tests -q",
        "--break", "drop the merge",
        "--reason", "the split was too fine",
    ], session=sid)
    if merged.returncode != 0:
        raise Failure(f"milestone merge failed: {merged.stderr[-800:]}")
    merged_id = merged.stdout.strip()
    if not merged_id.startswith("mil-"):
        raise Failure(f"merge printed {merged_id!r}, not a mil- id")
    after = run_foreman(["milestone", "list", name], session=sid)
    if after.returncode != 0:
        raise Failure(f"milestone list after merge failed: {after.stderr[-800:]}")
    lines = [line for line in after.stdout.splitlines() if line.strip()]
    live_lines = [line for line in lines if not line.startswith("change count")]
    if len(live_lines) != 1 or merged_id not in live_lines[0]:
        raise Failure(
            f"after merge list did not show only {merged_id}: {after.stdout[-800:]}")
    if "Parts together" not in after.stdout:
        raise Failure(f"list missed merged title: {after.stdout[-800:]}")
    if parts[0] in after.stdout or parts[1] in after.stdout:
        raise Failure(f"merge left a split id on the list: {after.stdout[-800:]}")
    return (
        f"add {source} without --reason named reason; "
        f"split {parts[0]} {parts[1]}; merge {merged_id} live only"
    )
