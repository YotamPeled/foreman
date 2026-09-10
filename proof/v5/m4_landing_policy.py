"""Clause 4.1: the repository's check beats the world's [merge] fallback."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

CLAUSE = ("4.1", "landing policy")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "landing.py"
_NEEDLE = '    if record is not None and record.get("shape") == "v5":\n'
_PATCH = '    if False and record is not None and record.get("shape") == "v5":\n'

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Per-repository policy replaces the global check.",
]
supervisor = "fake:high"
team = [
  "fake:high:1:supervisor",
  "fake:high:1:builder",
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "{work}"
target = "main"
check = "true"
land = "push"
trailers = ["Signed-off-by: Foreman"]
pr-body = "the front"
'''

_OLD = '''name      = "{name}"
order     = 1
want      = "Something the owner asked for in plain words."
done-when = "The thing works end to end on this machine."
land-on   = "main"
reviews   = "on request"
after     = []
prefer    = 0

[allocation]
muse = 1

[[task]]
title = "first"
scope = """
WHAT: do the first thing.
INPUTS: the brief.
OUTPUTS: the artifact.
OUT OF SCOPE: everything else.
"""
verify = "true"
size = 1
after = []
'''


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True)
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
    for item in cache.iterdir():
        if item.name.startswith(path.stem + "."):
            item.unlink(missing_ok=True)


def _false_config(root: Path) -> Path:
    """A copy of the world's config whose [merge] check says false.

    Clause 4.1 needs the fallback to disagree with the repository; the
    rest of the world keeps its own config.
    """
    alt = root / "m4-policy-config"
    if alt.is_dir():
        return alt
    live = Path(os.environ["FOREMAN_CONFIG"])
    shutil.copytree(live, alt)
    toml = alt / "foreman.toml"
    text = toml.read_text(encoding="utf-8")
    if '[merge]\ncheck = "true"\n' not in text:
        raise Failure("world config has no [merge] check = \"true\" to flip")
    toml.write_text(
        text.replace('[merge]\ncheck = "true"\n',
                     '[merge]\ncheck = "false"\n', 1),
        encoding="utf-8")
    return alt


def _bare(root: Path) -> Path:
    bare = root / "m4-policy" / "remote.git"
    if bare.is_dir():
        return bare
    src = root / "m4-policy" / "src"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "proof@example.invalid")
    _git(src, "config", "user.name", "proof")
    _git(src, "commit", "-q", "--allow-empty", "-m", "seed")
    proc = subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(bare)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"bare clone failed: {proc.stderr[-400:]}")
    return bare


def _add(directory: Path, body: str, env: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(body, encoding="utf-8")
    added = run_foreman(["front", "add", str(directory)], extra_env=env)
    if added.returncode != 0:
        raise Failure(f"front add {directory.name} failed: {added.stderr[-800:]}")


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure("BREAK apply: policy_for's v5 guard is not where it was")
    world["_41_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_41_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = ("policy_for stops reading the v5 repository entry", apply, restore)


def run(world) -> str:
    assert_world()
    root = Path(world["repo_a"]).parent
    alt = _false_config(root)
    env = {"FOREMAN_CONFIG": str(alt)}
    bare = _bare(root)
    n = world.get("_41_n", 0) + 1
    world["_41_n"] = n
    v5 = f"pol{n}"
    old = f"opol{n}"
    _add(Path(world["specs"]) / v5, _V5.format(
        name=v5, url=str(bare), work=f"w{v5}"), env)
    _add(Path(world["specs"]) / old, _OLD.format(name=old), env)

    shown = run_foreman(["front", "policy", v5, "foreman"], extra_env=env)
    if shown.returncode != 0:
        raise Failure(f"front policy {v5} failed: {shown.stderr[-800:]}")
    check = _field(shown.stdout, "check:")
    source = _field(shown.stdout, "source:")
    if check != "true" or source != "repository foreman":
        raise Failure(
            f"v5 front printed check {check!r} from {source!r}, "
            f"not the repository's 'true'")
    if _field(shown.stdout, "work:") != f"w{v5}":
        raise Failure(f"front policy lost the work branch:\n{shown.stdout[-800:]}")

    fell_back = run_foreman(["front", "policy", old, "foreman"], extra_env=env)
    if fell_back.returncode != 0:
        raise Failure(f"front policy {old} failed: {fell_back.stderr[-800:]}")
    old_check = _field(fell_back.stdout, "check:")
    old_source = _field(fell_back.stdout, "source:")
    if old_check != "false" or old_source != "[merge] fallback":
        raise Failure(
            f"old-shape front printed check {old_check!r} from "
            f"{old_source!r}, not the [merge] fallback 'false'")
    return (
        f"{v5} takes check 'true' from repository foreman while "
        f"[merge] says false; {old} takes 'false' from the [merge] fallback"
    )
