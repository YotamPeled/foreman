"""Clause 2.4: node prove records proven, unproven, and BREAK SURVIVED."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUSE = ("2.4", "node_prove")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "node.py"
_NEEDLE = (
    "    if base_run[\"saw\"] == \"red\" and head_run[\"saw\"] == \"green\":\n"
    "        verdict = \"proven\"\n"
)
_PATCH = (
    "    if True or (base_run[\"saw\"] == \"red\" and head_run[\"saw\"] == \"green\"):\n"
    "        verdict = \"proven\"\n"
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

PROOF_VERIFY = "test -f proof.txt && echo seen"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True)


def _git_ok(repo: Path, *args: str) -> str:
    proc = _git(repo, *args)
    if proc.returncode != 0:
        raise Failure(
            f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout)[-400:]}")
    return proc.stdout.strip()


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
    stored = world.get("_24_bare")
    if stored is not None:
        return Path(stored)
    root = Path(world["repo_a"]).parent / "m2-prove-front"
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
    world["_24_bare"] = str(bare)
    return bare


def _proof_repo(world) -> tuple[Path, str, str]:
    stored = world.get("_24_repo")
    if stored is not None:
        return Path(stored), world["_24_base"], world["_24_head"]
    root = Path(world["repo_a"]).parent / "m2-prove-repo"
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git_ok(repo, "init", "-q", "-b", "main")
    _git_ok(repo, "config", "user.email", "proof@example.invalid")
    _git_ok(repo, "config", "user.name", "proof")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git_ok(repo, "add", "seed.txt")
    _git_ok(repo, "commit", "-qm", "base")
    base = _git_ok(repo, "rev-parse", "HEAD")
    (repo / "proof.txt").write_text("proof\n", encoding="utf-8")
    (repo / "notes.txt").write_text("# note\n", encoding="utf-8")
    _git_ok(repo, "add", "proof.txt", "notes.txt")
    _git_ok(repo, "commit", "-qm", "head")
    head = _git_ok(repo, "rev-parse", "HEAD")
    world["_24_repo"] = str(repo)
    world["_24_base"] = base
    world["_24_head"] = head
    return repo, base, head


def _add_front(world, name: str, url: str) -> None:
    directory = Path(world["specs"]) / f"m2-pr-{name}"
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
    n = world.get("_24_n", 0) + 1
    world["_24_n"] = n
    name = f"pr{n}"
    _add_front(world, name, str(bare))
    return name, _register(name)


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_24_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_24_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "treat every node prove verdict as proven",
    apply, restore,
)


def _add_argv(front: str, parent: str, kind: str, title: str, *,
              break_patch: str | None = None) -> list[str]:
    argv = [
        "node", "add", front, "--parent", parent, "--kind", kind,
        "--title", title, "--verify", PROOF_VERIFY,
        "--must-not-touch", "the live state directory",
        "--reason", "the tree needs this node",
        "--break", "accept a green base",
        "--repo", "foreman",
    ]
    if break_patch is not None:
        argv.extend(["--break-patch", break_patch])
    return argv


def _add_ok(front: str, sid: str, parent: str, kind: str, title: str,
            *, break_patch: str | None = None) -> str:
    proc = run_foreman(
        _add_argv(front, parent, kind, title, break_patch=break_patch),
        session=sid)
    if proc.returncode != 0:
        raise Failure(
            f"node add {kind} {title!r} failed: {proc.stderr[-800:]}")
    return proc.stdout.strip()


def _comment_patch(repo: Path) -> str:
    (repo / "notes.txt").write_text("# other\n", encoding="utf-8")
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff"],
        capture_output=True, text=True)
    _git_ok(repo, "checkout", "--", ".")
    subprocess.run(
        ["git", "-C", str(repo), "clean", "-fdq"],
        capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise Failure("comment-only patch was empty")
    return proc.stdout


def run(world) -> str:
    assert_world()
    name, sid = _front(world)
    repo, base, head = _proof_repo(world)
    mil = _add_ok(name, sid, name, "milestone", "milestone one")
    task = _add_ok(name, sid, mil, "task", "the proof")
    proven = run_foreman([
        "node", "prove", name, task,
        "--base", base, "--head", head, "--repo", str(repo),
    ], session=sid)
    if proven.returncode != 0:
        raise Failure(
            f"node prove red/green failed: {proven.stderr[-800:]} {proven.stdout[-400:]}")
    if "proven" not in proven.stdout:
        raise Failure(f"red/green prove did not record proven: {proven.stdout[-800:]}")
    same = run_foreman([
        "node", "prove", name, task,
        "--base", head, "--head", head, "--repo", str(repo),
    ], session=sid)
    if same.returncode == 0:
        raise Failure("prove with head equal to base was admitted as proven")
    combined = (same.stdout or "") + (same.stderr or "")
    if "unproven" not in combined:
        raise Failure(f"same-ref prove did not record unproven: {combined[-800:]}")
    if "base: green" not in combined:
        raise Failure(
            f"same-ref unproven did not name the base's green run: {combined[-800:]}")
    patch = _comment_patch(repo)
    patch_file = Path(world["specs"]) / f"m2-prove-comment-{world['_24_n']}.patch"
    patch_file.write_text(patch, encoding="utf-8")
    survived_task = _add_ok(
        name, sid, mil, "task", "survived break",
        break_patch=str(patch_file))
    survived = run_foreman([
        "node", "prove", name, survived_task,
        "--base", base, "--head", head, "--repo", str(repo),
    ], session=sid)
    if "BREAK SURVIVED" not in survived.stdout:
        raise Failure(
            f"surviving break_patch did not print BREAK SURVIVED: "
            f"{survived.stdout[-800:]} {survived.stderr[-400:]}")
    return (
        f"{task} proven red/green; same-ref unproven naming base: green; "
        f"{survived_task} printed BREAK SURVIVED"
    )
