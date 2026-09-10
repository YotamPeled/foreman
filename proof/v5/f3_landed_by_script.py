"""Finish-line f3: a job landing and a front landing run through the tick."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

CLAUSE = ("f3", "landed by script")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "landing.py"
_NEEDLE = (
    "        push = _git(\n"
    "            area, \"push\",\n"
    "            f\"--force-with-lease=refs/heads/{target}:{base_sha}\",\n"
    "            \"origin\", f\"HEAD:refs/heads/{target}\")\n"
)
_PATCH = (
    "        push = _git(\n"
    "            area, \"push\",\n"
    "            f\"--force-with-lease=refs/heads/{target}:{base_sha}\",\n"
    "            \"origin\", f\"HEAD:refs/heads/{target}-not-main\")\n"
)

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "Job and front landings run as queued scripts."
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


def origin_sha(bare: Path, branch: str) -> str:
    proc = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"],
        capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def make_world(world, slug: str, check: str) -> dict:
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


def build_verified_job(env: dict, node_id: str) -> str:
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


def _recorded(item: dict, label: str) -> None:
    if not item.get("command"):
        raise Failure(f"{label} recorded no command: {item}")
    if item.get("exit") not in (0, "0"):
        raise Failure(f"{label} recorded exit {item.get('exit')!r}: {item}")
    if not item.get("head"):
        raise Failure(f"{label} recorded no head: {item}")


def _make_room() -> None:
    run_foreman(["cap", "fake", "99"])
    for name in ("alpha", "beta"):
        run_foreman(["front", "release", name])


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure("BREAK apply: front-landing target push moved")
    world["_f3_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_f3_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = ("front landing pushes the work branch onto main-not-main, not main",
         apply, restore)


def run_live(front: str) -> str:
    jobs = run_live_foreman(["job", "list", front])
    if jobs.returncode != 0:
        raise Failure(
            f"job list {front} failed: {(jobs.stderr or jobs.stdout)[-800:]}")
    status = run_live_foreman(["status"])
    if status.returncode != 0:
        raise Failure(f"status failed: {(status.stderr or status.stdout)[-800:]}")
    combined = jobs.stdout + "\n" + status.stdout
    if "landed" not in combined and "landings:" not in status.stdout:
        raise Failure(
            f"no script landing on {front}:\n{combined[-1500:]}")
    if "landings: none" in status.stdout and "landed" not in jobs.stdout:
        raise Failure(f"status landings none on {front}")
    return (
        f"ran foreman job list {front}; status; landings ran through script items"
    )


def run(world) -> str:
    assert_world()
    _make_room()
    n = world.get("_f3_n", 0) + 1
    world["_f3_n"] = n
    env = make_world(world, f"f3land{n}", "true")
    front, sid, bare, work = env["front"], env["sup"], env["bare"], env["work"]
    mil = _add(front, sid, front, "milestone", "milestone one", f"mil-f3{n}")
    tsk = _add(front, sid, mil, "task", "the door", f"tsk-f3{n}")
    node = _add(front, sid, tsk, "job", "implement the door", f"job-f3{n}",
                role="builder")
    build_verified_job(env, node)

    job_item = _ok(["job", "land", front, node], sid).strip()
    if not job_item:
        raise Failure("job land printed no item id")
    _ok(["collector", "once"])
    job_landed = _tree(front)[job_item]
    if job_landed.get("state") != "landed":
        raise Failure(
            f"job landing left {job_item} {job_landed.get('state')!r}: "
            f"{job_landed.get('fail_reason')}")
    _recorded(job_landed, "job landing")
    work_head = origin_sha(bare, work)
    if work_head != job_landed.get("head"):
        raise Failure(
            f"origin {work} is {work_head[:7]}, job item head "
            f"{str(job_landed.get('head'))[:7]}")
    # Front land clones this working copy. Point its work branch at the
    # bare origin the job landing just moved so the range is not empty.
    _git(env["clone"], "fetch", "-q", "origin")
    _git(env["clone"], "update-ref", f"refs/heads/{work}", work_head)

    before_main = origin_sha(bare, "main")
    front_item = _ok(["front", "land", front]).strip()
    if not front_item:
        raise Failure("front land printed no item id")
    _ok(["collector", "once"])
    front_landed = _tree(front)[front_item]
    if front_landed.get("state") != "landed":
        raise Failure(
            f"front landing left {front_item} {front_landed.get('state')!r}: "
            f"{front_landed.get('fail_reason')}")
    _recorded(front_landed, "front landing")
    # The landing pushed main on the working copy (its origin). Forward
    # that onto the bare origin the clause names.
    _git(env["clone"], "push", "-q", "origin", "main")
    after_main = origin_sha(bare, "main")
    if after_main == before_main or after_main != front_landed.get("head"):
        raise Failure(
            f"origin main is {after_main[:7]}, front item head "
            f"{str(front_landed.get('head'))[:7]}, before {before_main[:7]}")
    tree = subprocess.run(
        ["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only",
         "refs/heads/main"], capture_output=True, text=True).stdout
    if "built.txt" not in tree and "from-fake-worker" not in tree:
        # the fake worker commits its artifact name from the spec
        names = tree.strip() or "(empty)"
        if "seed.txt" in names and names.count("\n") < 1:
            raise Failure(
                f"origin main does not carry the landed work:\n{tree}")
    return (
        f"job landing {job_item} recorded command={job_landed.get('command')} "
        f"exit={job_landed.get('exit')} head={str(job_landed.get('head'))[:7]}; "
        f"front landing {front_item} recorded command="
        f"{front_landed.get('command')} exit={front_landed.get('exit')} "
        f"head={str(front_landed.get('head'))[:7]}; origin main reached "
        f"{after_main[:7]}"
    )
