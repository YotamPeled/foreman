"""Clause 4.3: front land pushes work onto the target; a moved target rebases."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLAUSE = ("4.3", "front landing")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "collector.py"
_NEEDLE = (
    "            recorded = _recorded_base_sha(record, entry)\n"
    "            if not recorded or recorded == sha:\n"
    "                continue\n"
)
_PATCH = _NEEDLE.replace(
    "            if not recorded or recorded == sha:\n",
    "            if True:\n", 1)

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Landing is a queued script.",
  "A target move invalidates recorded evidence.",
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
'''


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(
            f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout)[-400:]}")
    return proc.stdout.strip()


def _state() -> Path:
    return Path(os.environ["FOREMAN_STATE"])


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def _fold(rows: list[dict]) -> dict[str, dict]:
    by: dict[str, dict] = {}
    for row in rows:
        key = row.get("id")
        if isinstance(key, str) and key:
            by[key] = row
    return by


def _tree(front: str) -> dict[str, dict]:
    return _fold(_rows(_state() / "fronts" / front / "tree.jsonl"))


def _record(front: str) -> dict:
    rows = _rows(_state() / "fronts" / front / "front.jsonl")
    if not rows:
        raise Failure(f"no front record for {front}")
    return rows[-1]


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


def _ok(argv: list[str], session: str | None = None) -> str:
    proc = run_foreman(argv, session=session)
    if proc.returncode != 0:
        raise Failure(
            f"foreman {' '.join(argv[:3])} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[-800:]}")
    return proc.stdout


def origin_sha(bare: Path, branch: str) -> str:
    proc = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"],
        capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def rebase_items(front: str) -> list[dict]:
    return [node for node in _tree(front).values()
            if node.get("kind") == "rebase"]


def make_world(world, slug: str) -> dict:
    root = Path(world["repo_a"]).parent / slug
    src = root / "src"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "proof@example.invalid")
    _git(src, "config", "user.name", "proof")
    (src / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(src, "add", "seed.txt")
    _git(src, "commit", "-qm", "seed")
    bare = root / "remote.git"
    proc = subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(bare)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"bare clone failed: {proc.stderr[-400:]}")
    clone = root / "clone"
    proc = subprocess.run(
        ["git", "clone", "-q", str(bare), str(clone)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"clone failed: {proc.stderr[-400:]}")
    _git(clone, "config", "user.email", "proof@example.invalid")
    _git(clone, "config", "user.name", "proof")
    work = f"w{slug}"
    front = slug
    directory = Path(world["specs"]) / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(name=front, url=str(bare), work=work), encoding="utf-8")
    # front add refuses a work branch that already exists on the remote.
    _ok(["front", "add", str(directory)])
    _git(clone, "checkout", "-qb", work)
    (clone / "feat.txt").write_text("feat\n", encoding="utf-8")
    _git(clone, "add", "feat.txt")
    _git(clone, "commit", "-qm", "work ahead")
    _git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{work}")
    sid = _field(_ok(["register", "--role", "supervisor", "--front", front,
                      "--pid", str(os.getpid())]), "session:")
    _ok(["node", "add", front, "--parent", front, "--kind", "milestone",
         "--title", "milestone one",
         "--verify", "python -m pytest tests -q",
         "--must-not-touch", "the live state directory",
         "--reason", "the tree needs this node",
         "--break", "admit a job as a parent",
         "--repo", "foreman"], sid)
    return {"root": root, "bare": bare, "clone": clone, "work": work,
            "front": front, "sup": sid}


def move_origin_main(clone: Path, name: str) -> str:
    _git(clone, "fetch", "-q", "origin")
    _git(clone, "checkout", "-q", "-B", "main", "origin/main")
    (clone / name).write_text("main\n", encoding="utf-8")
    _git(clone, "add", name)
    _git(clone, "commit", "-qm", "target moved")
    _git(clone, "push", "-q", "origin", "main")
    return _git(clone, "rev-parse", "HEAD")


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure("BREAK apply: _detect_target_moves' guard moved")
    world["_43_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_43_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = ("_detect_target_moves never sees a moved target", apply, restore)


def run(world) -> str:
    assert_world()
    n = world.get("_43_n", 0) + 1
    world["_43_n"] = n
    env = make_world(world, f"m4front{n}")
    front, sid, bare, work = env["front"], env["sup"], env["bare"], env["work"]

    item = _ok(["front", "land", front]).strip()
    if not item:
        raise Failure("front land printed no item id")
    queued = _tree(front)[item]
    if (queued.get("kind") != "front-landing" or queued.get("role") != "script"
            or queued.get("lands") != work or queued.get("target") != "main"
            or queued.get("state") != "queued"):
        raise Failure(f"front land queued the wrong item: {queued}")
    before_main = origin_sha(bare, "main")
    work_sha = origin_sha(bare, work)
    _ok(["collector", "once"])
    landed = _tree(front)[item]
    after_main = origin_sha(bare, "main")
    if landed.get("state") != "landed":
        raise Failure(
            f"one tick left {item} {landed.get('state')!r}: "
            f"{landed.get('fail_reason')}")
    if after_main == before_main or after_main != landed.get("head"):
        raise Failure(
            f"origin main is {after_main[:7]}, item head "
            f"{str(landed.get('head'))[:7]}, before {before_main[:7]}")
    tree = subprocess.run(
        ["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only",
         "refs/heads/main"], capture_output=True, text=True).stdout
    if "feat.txt" not in tree:
        raise Failure(f"origin main does not carry the work commit:\n{tree}")

    moved = move_origin_main(env["clone"], f"moved{n}.txt")
    _ok(["collector", "once"])
    items = rebase_items(front)
    if len(items) != 1:
        raise Failure(
            f"a moved target queued {len(items)} rebase items, not one")
    rebase = items[0]
    if (rebase.get("state") != "queued" or rebase.get("role") != "script"
            or rebase.get("lands") != work or rebase.get("onto") != moved):
        raise Failure(f"the rebase item is wrong: {rebase}")
    if _record(front).get("behind") != moved:
        raise Failure(
            f"the front record's behind is {_record(front).get('behind')!r}, "
            f"not {moved}")
    refused = run_foreman(["front", "done", front], session=sid)
    if refused.returncode == 0:
        raise Failure("front done succeeded while the front was behind")
    err = refused.stderr
    for needle in ("main", moved, str(rebase.get("id")), "rebase", "queued"):
        if needle not in err:
            raise Failure(f"front done refusal never says {needle!r}: {err[-800:]}")

    _ok(["collector", "once"])
    ran = _tree(front)[str(rebase.get("id"))]
    if ran.get("state") != "landed":
        raise Failure(
            f"the rebase item is {ran.get('state')!r}: {ran.get('fail_reason')}")
    record = _record(front)
    if record.get("base_sha") != moved:
        raise Failure(
            f"base_sha is {record.get('base_sha')!r}, not the moved {moved}")
    if str(record.get("behind") or ""):
        raise Failure(f"behind is still {record.get('behind')!r}")
    done = run_foreman(["front", "done", front], session=sid)
    if done.returncode != 0:
        raise Failure(
            f"front done still refused after the rebase: {done.stderr[-800:]}")
    if done.stdout.strip() != f"{front} done":
        raise Failure(f"front done printed {done.stdout.strip()!r}")
    return (
        f"front land moved origin main to {after_main[:7]}; a commit behind "
        f"the front's back queued one rebase item onto {moved[:7]} and "
        f"front done named main, {moved[:7]} and {rebase.get('id')}; the "
        f"rebase moved base_sha and front done then printed '{front} done'"
    )
