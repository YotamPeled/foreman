"""Clause 4.5: evidence is bound to head and base; a rebase stales it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

CLAUSE = ("4.5", "evidence binding")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "landing.py"
_NEEDLE = "        name = matching_flake(front, node_id, output)\n"
_PATCH = "        name = None\n"

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
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
check = "{check}"
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


def _jobs(front: str) -> dict[str, dict]:
    return _fold(_rows(_state() / "fronts" / front / "jobs.jsonl"))


def _record(front: str) -> dict:
    rows = _rows(_state() / "fronts" / front / "front.jsonl")
    if not rows:
        raise Failure(f"no front record for {front}")
    return rows[-1]


def _base_sha(front: str) -> str:
    """The base the front was admitted on: the record, else its repository."""
    record = _record(front)
    sha = str(record.get("base_sha") or "").strip()
    if sha:
        return sha
    for entry in record.get("repositories") or []:
        if isinstance(entry, dict) and str(entry.get("base_sha") or "").strip():
            return str(entry["base_sha"]).strip()
    return ""


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


def make_world(world, slug: str, *, url_is_clone: bool,
               check: str = "true") -> dict:
    """A bare origin and a working clone beside it.

    ``url_is_clone`` names the clone as the front's repository so a job
    landing cuts its worktree from a checkout and pushes on to the bare
    origin; otherwise the bare is the repository a front landing or a
    rebase clones.
    """
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
        _V5.format(name=front, url=str(clone if url_is_clone else bare),
                   work=work, check=check),
        encoding="utf-8")
    # front add refuses a work branch that already exists on the remote.
    _ok(["front", "add", str(directory)])
    _git(clone, "checkout", "-qb", work)
    _git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{work}")
    _git(clone, "fetch", "-q", "origin", work)
    if not url_is_clone:
        # A queued job's worktree is cut from the bare, whose own origin
        # is src: the launcher fetches work from there before branching.
        _git(clone, "push", "-q", str(src), f"HEAD:refs/heads/{work}")
        proc = subprocess.run(
            ["git", "--git-dir", str(bare), "fetch", "-q", "origin",
             f"+refs/heads/{work}:refs/remotes/origin/{work}"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise Failure(f"bare fetch of {work} failed: {proc.stderr[-400:]}")
    sid = _field(_ok(["register", "--role", "supervisor", "--front", front,
                      "--pid", str(os.getpid())]), "session:")
    return {"root": root, "bare": bare, "clone": clone, "work": work,
            "front": front, "sup": sid}


def _add(front: str, sid: str, parent: str, kind: str, title: str,
         role: str | None = None) -> str:
    argv = ["node", "add", front, "--parent", parent, "--kind", kind,
            "--title", title, "--verify", "python -m pytest tests -q",
            "--must-not-touch", "the live state directory",
            "--reason", "the tree needs this node",
            "--break", "admit a job as a parent",
            "--repo", "foreman"]
    if role is not None:
        argv.extend(["--role", role])
    return _ok(argv, sid).strip()


def build_returned_job(env: dict, node_id: str) -> str:
    """Queue the node and let the fake worker return its job."""
    front, sid = env["front"], env["sup"]
    _ok(["job", "queue", front, node_id], sid)
    _ok(["collector", "once"])
    job_id = ""
    for _ in range(60):
        node = _tree(front).get(node_id) or {}
        job_id = str(node.get("job") or "")
        if job_id:
            record = _jobs(front).get(job_id) or {}
            if str(record.get("state") or "") in (
                    "returned", "failed", "died", "verified"):
                break
        time.sleep(0.25)
        _ok(["collector", "once"])
    else:
        node = _tree(front).get(node_id) or {}
        raise Failure(
            f"{node_id} never returned a job (job {job_id or 'unset'}, "
            f"state {node.get('state')!r}, waits {node.get('waits')!r})")
    record = _jobs(front).get(job_id) or {}
    if str(record.get("state") or "") != "returned":
        raise Failure(f"job {job_id} is {record.get('state')!r}, not returned")
    return job_id


def move_origin_main(clone: Path, name: str) -> str:
    _git(clone, "fetch", "-q", "origin")
    _git(clone, "checkout", "-q", "-B", "main", "origin/main")
    (clone / name).write_text("main\n", encoding="utf-8")
    _git(clone, "add", name)
    _git(clone, "commit", "-qm", "target moved")
    _git(clone, "push", "-q", "origin", "main")
    return _git(clone, "rev-parse", "HEAD")


def write_flake_check(root: Path) -> str:
    """A check that fails one named test on its first run and passes next."""
    script = root / "flake_check.py"
    counter = root / "flake_runs.txt"
    script.write_text(
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "n += 1\n"
        "p.write_text(str(n))\n"
        "if n == 1:\n"
        "    print('FAILED tests/test_x.py::test_flaky')\n"
        "    sys.exit(1)\n"
        "print('ok')\n",
        encoding="utf-8")
    return f"{sys.executable} {script}"


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure("BREAK apply: the flake lookup moved")
    world["_45_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_45_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = ("a landing check never recognises a registered flake", apply, restore)


def _bound_and_stale(world, n: int) -> tuple[str, str, str]:
    env = make_world(world, f"m4bind{n}", url_is_clone=False)
    front, sid = env["front"], env["sup"]
    base = _base_sha(front)
    if not base:
        raise Failure("front add recorded no base_sha to bind evidence to")
    mil = _add(front, sid, front, "milestone", "milestone one")
    tsk = _add(front, sid, mil, "task", "the door")
    node = _add(front, sid, tsk, "job", "implement the door", role="builder")
    job_id = build_returned_job(env, node)
    head = str((_jobs(front).get(job_id) or {}).get("head") or "")
    if not head:
        raise Failure(f"job {job_id} recorded no head")
    _ok(["job", "verify", job_id, "--confirmed", "--command", "true",
         "--output", "the fake worker committed its artifact"], sid)
    listed = _ok(["evidence", "list", front], sid)
    label = f"bound {head[:7]}/{base[:7]}"
    if label not in listed:
        raise Failure(f"evidence list has no {label!r}:\n{listed[-800:]}")
    _ok(["node", "revise", front, node, "--state", "landed",
         "--reason", "the job landed"], sid)

    moved = move_origin_main(env["clone"], f"moved{n}.txt")
    _ok(["collector", "once"])
    items = rebase_items(front)
    if len(items) != 1:
        raise Failure(f"a moved target queued {len(items)} rebase items")
    _ok(["collector", "once"])
    ran = _tree(front)[str(items[0].get("id"))]
    if ran.get("state") != "landed":
        raise Failure(
            f"the rebase item is {ran.get('state')!r}: {ran.get('fail_reason')}")
    if _base_sha(front) != moved:
        raise Failure("the rebase did not move base_sha")
    listed = _ok(["evidence", "list", front], sid)
    stale = f"stale (base moved to {moved[:7]})"
    if stale not in listed:
        raise Failure(f"evidence list has no {stale!r}:\n{listed[-800:]}")
    refused = run_foreman(["front", "done", front], session=sid)
    if refused.returncode == 0:
        raise Failure("front done succeeded on stale evidence")
    want = f"front {front} has stale evidence on {node}; re-run its verify"
    if want not in refused.stderr:
        raise Failure(
            f"front done never says {want!r}: {refused.stderr[-800:]}")
    return front, node, moved


def _flake_landing(world, n: int) -> tuple[str, str, str]:
    slug = f"m4flake{n}"
    root = Path(world["repo_a"]).parent / slug
    root.mkdir(parents=True, exist_ok=True)
    check = write_flake_check(root)
    env = make_world(world, slug, url_is_clone=True, check=check)
    front, sid = env["front"], env["sup"]
    mil = _add(front, sid, front, "milestone", "milestone one")
    tsk = _add(front, sid, mil, "task", "the door")
    node = _add(front, sid, tsk, "job", "implement the door", role="builder")
    job_id = build_returned_job(env, node)
    _ok(["job", "verify", job_id, "--confirmed", "--command", "true",
         "--output", "the fake worker committed its artifact"], sid)
    _ok(["check", "flake", front, node, "--test", "test_flaky",
         "--reason", "fails under contention"], sid)
    item = _ok(["job", "land", front, node], sid).strip()
    before = origin_sha(env["bare"], env["work"])
    _ok(["collector", "once"])
    landed = _tree(front)[item]
    if landed.get("state") != "landed":
        raise Failure(
            f"the flaky landing is {landed.get('state')!r}: "
            f"{landed.get('fail_reason')}")
    if landed.get("greens") != 1 or landed.get("runs") != 2 \
            or landed.get("flake") != "test_flaky":
        raise Failure(
            f"the item recorded greens/runs/flake "
            f"{landed.get('greens')!r}/{landed.get('runs')!r}/"
            f"{landed.get('flake')!r}, not 1/2/test_flaky")
    after = origin_sha(env["bare"], env["work"])
    if after == before or after != landed.get("head"):
        raise Failure(
            f"origin {env['work']} is {after[:7]}, not the landed head")
    listed = _ok(["job", "list", front], sid)
    line = f"{item}  script    green 1/2 (flake: test_flaky)"
    if line not in listed.splitlines():
        raise Failure(f"job list has no {line!r}:\n{listed[-800:]}")
    runs = (root / "flake_runs.txt").read_text(encoding="utf-8")
    if runs != "2":
        raise Failure(f"the check ran {runs} times, not 2")
    return front, item, after


def run(world) -> str:
    assert_world()
    isolate_queued_fronts()
    n = world.get("_45_n", 0) + 1
    world["_45_n"] = n
    bound_front, node, moved = _bound_and_stale(world, n)
    flake_front, item, head = _flake_landing(world, n)
    return (
        f"{bound_front} listed its verify bound to head and base, then "
        f"stale at {moved[:7]} with front done naming {node}; {flake_front} "
        f"retried its registered flake once, recorded green 1/2 and pushed "
        f"{head[:7]}"
    )
