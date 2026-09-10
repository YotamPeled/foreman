"""Clause 3.6: quota death, redesign after two rounds, muse mechanical."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLAUSE = ("3.6", "review_states")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "collector.py"
_NEEDLE = (
    "        if kind == \"access\":\n"
    "            return \"access\"\n"
    "        return \"quota\"\n"
)
_PATCH = (
    "        if kind == \"access\":\n"
    "            return \"access\"\n"
    "        return None\n"
)

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
]
supervisor = "{supervisor}"
team = [
{team}
]

[[repository]]
name = "foreman"
url = "{url}"
base = "main"
work = "{work}"
target = "main"
check = "python -m pytest tests -q"
'''

VERDICT_CORRECTNESS = (
    '{"passed": false, "summary": "wrong", "class": "correctness",'
    ' "findings": [{"title": "bug", "detail": "x", "class": "correctness"}]}'
)
QUOTA_LOG = (
    "API error 429 quota exhausted resets at 2026-09-14T00:00:00Z\n"
    "### finished rc=0\n"
)
CLEAN_LOG = "### finished rc=0\n"


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
    stored = world.get("_36_bare")
    if stored is not None:
        return Path(stored)
    root = Path(world["repo_a"]).parent / "m3-rv"
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
    world["_36_bare"] = str(bare)
    return bare


def _add_front(world, name: str, url: str, work: str, *,
               supervisor: str, team: str) -> None:
    directory = Path(world["specs"]) / f"m3-rv-{name}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(name=name, url=url, work=work,
                   supervisor=supervisor, team=team),
        encoding="utf-8")
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


def _add_ok(front: str, sid: str, parent: str, kind: str, title: str,
            **flags) -> str:
    argv = [
        "node", "add", front, "--parent", parent, "--kind", kind,
        "--title", title,
        "--verify", "python -m pytest tests -q",
        "--must-not-touch", "the live state directory",
        "--reason", "the tree needs this node",
        "--break", "admit a job as a parent",
        "--repo", "foreman",
    ]
    if flags.get("role") is not None:
        argv.extend(["--role", flags["role"]])
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    proc = run_foreman(argv, session=sid)
    if proc.returncode != 0:
        raise Failure(
            f"node add {kind} {title!r} failed: {proc.stderr[-800:]}")
    return proc.stdout.strip()


def _state() -> Path:
    return Path(os.environ["FOREMAN_STATE"])


def _append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _latest_node(front: str, nid: str) -> dict:
    path = _state() / "fronts" / front / "tree.jsonl"
    latest = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("id") == nid:
            latest = row
    if latest is None:
        raise Failure(f"node {nid} not on the tree")
    return latest


def _bind_job(front: str, nid: str, job_id: str, session: str) -> None:
    node = _latest_node(front, nid)
    path = _state() / "fronts" / front / "tree.jsonl"
    _append_jsonl(path, dict(node, state="running", job=job_id,
                             session=session, op="revise", waits=""))


def _write_job(front: str, job_id: str, session: str, task: str) -> None:
    _append_jsonl(_state() / "fronts" / front / "jobs.jsonl", {
        "id": job_id, "task": task, "kind": "review", "role": "astra",
        "state": "running", "session": session,
        "started_at": "2026-09-10T11:55:00+00:00",
    })


def _write_session(sid: str, front: str, job_id: str, pid: int,
                   log_text: str, verdict: str | None) -> None:
    session_dir = _state() / "sessions" / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "log"
    log_path.write_text(log_text, encoding="utf-8")
    if verdict is not None:
        (session_dir / "verdict.json").write_text(verdict, encoding="utf-8")
    roster_path = _state() / "roster.json"
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    sessions = roster.setdefault("sessions", {})
    sessions[sid] = {
        "id": sid, "role": "astra", "pool": "grok", "model": "grok-4.6",
        "front": front, "job": job_id, "pid": pid, "pgid": pid,
        "worktree": "", "log": str(log_path), "timeout": "20m",
        "launched_by": "owner",
        "started_at": "2026-09-10T11:55:00+00:00",
        "state": "running",
    }
    roster_path.write_text(json.dumps(roster), encoding="utf-8")


def _write_fake_muse() -> None:
    dest = Path(os.environ["FOREMAN_CONFIG"]) / "pools" / "fakemuse"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.toml").write_text(
        'name = "fakemuse"\n'
        'model = "fake-muse-model"\n'
        'timeout_default = "5m"\n'
        'effort_default = "xhigh"\n'
        'effort_min = "xhigh"\n'
        'interactive = false\n'
        'roles = ["muse"]\n'
        'adapter = "grok"\n',
        encoding="utf-8")
    (dest / "SKILL.md").write_text("# fakemuse\n\nProof muse-shaped pool.\n",
                                   encoding="utf-8")


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_36_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_36_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "a quota refusal is not recorded as died (quota)",
    apply, restore,
)


def run(world) -> str:
    assert_world()
    bare = _bare(world)
    n = world.get("_36_n", 0) + 1
    world["_36_n"] = n
    name = f"rv{n}"
    work = f"w{name}"
    team = (
        '  "grok-4.6:high:1:supervisor",\n'
        '  "grok-4.6:high:1:reviewer",'
    )
    _add_front(world, name, str(bare), work,
               supervisor="grok-4.6:high", team=team)
    sid = _register(name)
    mil = _add_ok(name, sid, name, "milestone", "milestone one",
                  node_id=f"mil-rv{n}")
    tsk = _add_ok(name, sid, mil, "task", "the door",
                  node_id=f"tsk-rv{n}")
    quota_node = _add_ok(name, sid, tsk, "job", "review quota",
                         role="reviewer", node_id=f"job-rvq{n}")
    redesign_node = _add_ok(name, sid, tsk, "job", "review twice",
                            role="reviewer", node_id=f"job-rvr{n}")
    job_q = f"job-q{n}0001"
    job_1 = f"job-r{n}0001"
    job_2 = f"job-r{n}0002"
    ses_q = f"ses-q{n}0001"
    ses_1 = f"ses-r{n}0001"
    ses_2 = f"ses-r{n}0002"
    _write_job(name, job_q, ses_q, tsk)
    _write_job(name, job_1, ses_1, tsk)
    _write_job(name, job_2, ses_2, tsk)
    _bind_job(name, quota_node, job_q, ses_q)
    _bind_job(name, redesign_node, job_1, ses_1)
    _bind_job(name, redesign_node, job_2, ses_2)
    _write_session(ses_q, name, job_q, 2_000_000 + n, QUOTA_LOG, None)
    _write_session(ses_1, name, job_1, 2_000_100 + n, CLEAN_LOG,
                   VERDICT_CORRECTNESS)
    _write_session(ses_2, name, job_2, 2_000_200 + n, CLEAN_LOG,
                   VERDICT_CORRECTNESS)
    tick = run_foreman(["collector", "once"])
    if tick.returncode != 0:
        raise Failure(f"collector once failed: {tick.stderr[-800:]}")
    listed = run_foreman(["job", "list", name], session=sid)
    if listed.returncode != 0:
        raise Failure(f"job list failed: {listed.stderr[-800:]}")
    out = listed.stdout
    if "died (quota)" not in out:
        raise Failure(f"quota review was not died (quota):\n{out[-1500:]}")
    if "redesign (twice: correctness)" not in out:
        raise Failure(
            f"two correctness rounds did not set redesign:\n{out[-1500:]}")
    _write_fake_muse()
    muse_name = f"mu{n}"
    muse_work = f"w{muse_name}"
    muse_team = (
        '  "grok-4.6:high:1:supervisor",\n'
        '  "fakemuse:xhigh:1:builder",'
    )
    _add_front(world, muse_name, str(bare), muse_work,
               supervisor="grok-4.6:high", team=muse_team)
    muse_sid = _register(muse_name)
    mil_m = _add_ok(muse_name, muse_sid, muse_name, "milestone",
                    "milestone one", node_id=f"mil-mu{n}")
    tsk_m = _add_ok(muse_name, muse_sid, mil_m, "task", "the door",
                    node_id=f"tsk-mu{n}")
    leaf = _add_ok(muse_name, muse_sid, tsk_m, "job", "not mechanical",
                   role="builder", node_id=f"job-mu{n}")
    refused = run_foreman(
        ["job", "queue", muse_name, leaf], session=muse_sid)
    if refused.returncode == 0:
        raise Failure("non-mechanical muse node was queued")
    err = refused.stderr or ""
    if "mechanical" not in err:
        raise Failure(
            f"muse refusal did not name mechanical: {err[-800:]}")
    return (
        f"{job_q} died (quota); {redesign_node} redesign (twice: correctness); "
        f"{leaf} refused naming mechanical"
    )
