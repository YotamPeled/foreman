"""Clause 4.2: job land queues a script item; one tick lands it."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

CLAUSE = ("4.2", "landing script")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "landing.py"
# The same red-check guard is written in three landing runners; the
# job landing's is the one whose next statement is the work-branch push.
_NEEDLE = (
    "        if exit_code != 0:\n"
    "            return _fail(\n"
    "                f\"check '{command}' failed (exit {exit_code})\",\n"
    "                **fields)\n"
    "        push = _git(\n"
    "            area, \"push\",\n"
    "            f\"--force-with-lease=refs/heads/{work}:{base_sha}\",\n"
)
_PATCH = _NEEDLE.replace("        if exit_code != 0:\n",
                         "        if False:\n", 1)

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Landing is a queued script.",
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


def _drop_pyc(path: Path) -> None:
    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return
    for item in cache.iterdir():
        if item.name.startswith(path.stem + "."):
            item.unlink(missing_ok=True)


def _field(out: str, prefix: str) -> str:
    needle = prefix if prefix.endswith(" ") else prefix + " "
    for line in out.splitlines():
        if line.startswith(needle):
            return line[len(needle):].strip()
    raise Failure(f"no {prefix!r} line in output:\n{out[-2000:]}")


def _ok(argv: list[str], session: str | None = None) -> str:
    proc = run_foreman(argv, session=session)
    if proc.returncode != 0:
        raise Failure(
            f"foreman {' '.join(argv[:3])} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[-800:]}")
    return proc.stdout


def make_world(world, slug: str, check: str) -> dict:
    """A bare origin with work, a working clone as the front's repository.

    The clone is what a queued job's worktree is cut from; the bare is
    the origin a landing pushes to.
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
    front = slug.replace("-", "")
    directory = Path(world["specs"]) / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(name=front, url=str(clone), work=work, check=check),
        encoding="utf-8")
    # front add refuses a work branch that already exists on the remote,
    # so the branch is cut after the front is admitted.
    _ok(["front", "add", str(directory)])
    _git(clone, "checkout", "-qb", work)
    _git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{work}")
    _git(clone, "fetch", "-q", "origin", work)
    sid = _field(_ok(["register", "--role", "supervisor", "--front", front,
                      "--pid", str(os.getpid())]), "session:")
    return {"root": root, "bare": bare, "clone": clone, "work": work,
            "front": front, "sup": sid}


def _add(front: str, sid: str, parent: str, kind: str, title: str,
         node_id: str, role: str | None = None) -> str:
    argv = ["node", "add", front, "--parent", parent, "--kind", kind,
            "--title", title, "--verify", "python -m pytest tests -q",
            "--must-not-touch", "the live state directory",
            "--reason", "the tree needs this node",
            "--break", "admit a job as a parent",
            "--repo", "foreman", "--id", node_id]
    if role is not None:
        argv.extend(["--role", role])
    return _ok(argv, sid).strip()


def origin_sha(bare: Path, branch: str) -> str:
    proc = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"],
        capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def build_verified_job(env: dict, node_id: str) -> str:
    """Queue the node, let the fake worker return it, verify it.

    The worker commits one file on ``job/<front>-<node>`` in the clone,
    so the branch is one commit ahead of the work branch.
    """
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
        raise Failure(
            f"job {job_id} is {record.get('state')!r}, not returned")
    _ok(["job", "verify", job_id, "--confirmed", "--command", "true",
         "--output", "the fake worker committed its artifact"], sid)
    verified = _jobs(front).get(job_id) or {}
    if str(verified.get("state") or "") != "verified":
        raise Failure(f"job {job_id} did not become verified")
    return job_id


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure("BREAK apply: _run_locked's red-check guard moved")
    world["_42_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_42_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = ("_run_locked pushes even when the check exits non-zero",
         apply, restore)


def run(world) -> str:
    assert_world()
    n = world.get("_42_n", 0) + 1
    world["_42_n"] = n

    green = make_world(world, f"m4land{n}", "true")
    front, sid = green["front"], green["sup"]
    mil = _add(front, sid, front, "milestone", "milestone one", f"mil-l{n}")
    tsk = _add(front, sid, mil, "task", "the door", f"tsk-l{n}")
    node = _add(front, sid, tsk, "job", "implement the door", f"job-l{n}",
                role="builder")
    build_verified_job(green, node)
    item = _ok(["job", "land", front, node], sid).strip()
    if not item:
        raise Failure("job land printed no item id")
    queued = _tree(front)[item]
    if (queued.get("role") != "script" or queued.get("lands") != node
            or queued.get("state") != "queued"
            or queued.get("parent") != tsk):
        raise Failure(f"job land queued the wrong item: {queued}")
    before = origin_sha(green["bare"], green["work"])
    _ok(["collector", "once"])
    landed = _tree(front)[item]
    after = origin_sha(green["bare"], green["work"])
    if landed.get("state") != "landed":
        raise Failure(
            f"one tick left {item} {landed.get('state')!r}: "
            f"{landed.get('fail_reason')}")
    if after == before or after != landed.get("head"):
        raise Failure(
            f"origin {green['work']} is {after[:7]}, item head "
            f"{str(landed.get('head'))[:7]}, before {before[:7]}")
    if landed.get("command") != "true" or landed.get("exit") != 0:
        raise Failure(f"item recorded command/exit {landed}")
    seconds = landed.get("seconds")
    if not isinstance(seconds, (int, float)) or seconds < 0:
        raise Failure(f"item recorded seconds {seconds!r}")
    out_file = str(landed.get("output_file") or "")
    if not out_file or not Path(out_file).is_file() \
            or str(_state()) not in out_file:
        raise Failure(
            f"output file {out_file!r} is not a file under {_state()}")

    red = make_world(world, f"m4red{n}", "false")
    rfront, rsid = red["front"], red["sup"]
    rmil = _add(rfront, rsid, rfront, "milestone", "milestone one", f"mil-r{n}")
    rtsk = _add(rfront, rsid, rmil, "task", "the door", f"tsk-r{n}")
    rnode = _add(rfront, rsid, rtsk, "job", "implement the door", f"job-r{n}",
                 role="builder")
    build_verified_job(red, rnode)
    ritem = _ok(["job", "land", rfront, rnode], rsid).strip()
    rbefore = origin_sha(red["bare"], red["work"])
    _ok(["collector", "once"])
    failed = _tree(rfront)[ritem]
    if failed.get("state") != "failed":
        raise Failure(
            f"a red check left {ritem} {failed.get('state')!r}, not failed")
    if origin_sha(red["bare"], red["work"]) != rbefore:
        raise Failure("a red check still moved origin's work branch")
    if failed.get("exit") == 0 or failed.get("command") != "false":
        raise Failure(f"failed item recorded {failed}")
    return (
        f"one tick landed {item}: origin {green['work']} moved to "
        f"{after[:7]}, exit 0 in {seconds}s, output under the state "
        f"directory; the red check left {ritem} failed and origin at "
        f"{rbefore[:7]}"
    )
