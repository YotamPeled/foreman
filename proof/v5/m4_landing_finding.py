"""Clause 4.4: a red-check landing files a finding and wakes the supervisor."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

CLAUSE = ("4.4", "landing finding")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "landing.py"
_NEEDLE = '        sid, "landing failed", job=item_id, text=finding_id)\n'
_PATCH = '        sid, "job returned", job=item_id, text=finding_id)\n'

_CHECK = "echo LANDING-RED; false"

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "A landing failure reaches the supervisor as a finding, never the owner.",
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


def _findings(front: str) -> list[dict]:
    return _rows(_state() / "fronts" / front / "findings.jsonl")


def _events(sid: str) -> list[dict]:
    return _rows(_state() / "sessions" / sid / "events.jsonl")


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


def make_world(world, slug: str) -> dict:
    """A bare origin, a working clone as the front's repository, a red check."""
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
        _V5.format(name=front, url=str(clone), work=work, check=_CHECK),
        encoding="utf-8")
    # front add refuses a work branch that already exists on the remote.
    _ok(["front", "add", str(directory)])
    _git(clone, "checkout", "-qb", work)
    _git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{work}")
    _git(clone, "fetch", "-q", "origin", work)
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


def build_verified_job(env: dict, node_id: str) -> str:
    """Queue the node, let the fake worker return it, verify it."""
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
    _ok(["job", "verify", job_id, "--confirmed", "--command", "true",
         "--output", "the fake worker committed its artifact"], sid)
    return job_id


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure("BREAK apply: _wake_landing's failure event moved")
    world["_44_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_44_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = ("a failed landing wakes the supervisor with 'job returned'",
         apply, restore)


def run(world) -> str:
    assert_world()
    isolate_queued_fronts()
    n = world.get("_44_n", 0) + 1
    world["_44_n"] = n
    env = make_world(world, f"m4find{n}")
    front, sid = env["front"], env["sup"]
    mil = _add(front, sid, front, "milestone", "milestone one")
    tsk = _add(front, sid, mil, "task", "the door")
    node = _add(front, sid, tsk, "job", "implement the door", role="builder")
    build_verified_job(env, node)
    item = _ok(["job", "land", front, node], sid).strip()
    before = origin_sha(env["bare"], env["work"])
    _ok(["collector", "once"])
    landed = _tree(front)[item]
    if landed.get("state") != "failed":
        raise Failure(
            f"the red check left {item} {landed.get('state')!r}, not failed")
    if origin_sha(env["bare"], env["work"]) != before:
        raise Failure("the red check still moved origin's work branch")

    rows = _findings(front)
    if len(rows) != 1:
        raise Failure(f"a failed landing filed {len(rows)} findings, not one")
    finding = rows[0]
    if finding.get("class") != "landing" or finding.get("on") != front:
        raise Failure(f"the finding is {finding.get('class')!r} on "
                      f"{finding.get('on')!r}")
    ref = str(finding.get("evidence_ref") or "")
    if not ref or not Path(ref).is_file():
        raise Failure(f"the finding's evidence_ref {ref!r} is not a file")
    text = Path(ref).read_text(encoding="utf-8")
    if "LANDING-RED" not in text:
        raise Failure(
            f"the evidence file does not hold the check output:\n{text[-500:]}")
    if landed.get("finding") != finding.get("id"):
        raise Failure(
            f"the item names finding {landed.get('finding')!r}, not "
            f"{finding.get('id')!r}")
    seen = [(row.get("reason"), row.get("job"), row.get("text"))
            for row in _events(sid)]
    want = ("landing failed", item, finding.get("id"))
    if want not in seen:
        raise Failure(
            f"the supervisor's events are {seen}, without {want}")
    inbox = _rows(_state() / "inbox.jsonl")
    if inbox:
        raise Failure(f"the owner's inbox holds {len(inbox)} lines: {inbox[-1]}")
    return (
        f"the red check left {item} failed, filed one landing finding "
        f"{finding.get('id')} whose evidence file holds the check output, "
        f"woke {sid} with 'landing failed' naming both ids, and left the "
        f"inbox empty"
    )
