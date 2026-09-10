"""Clause 3.4: started job page names what, verify, branch and a map fact."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLAUSE = ("3.4", "worker_page")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "launch.py"
_NEEDLE = "    what = str(node.get(\"what\") or \"\").strip()\n"
_PATCH = "    what = \"\"\n"

_MUST = "src/foreman/store.py"
_VERIFY = "python -m pytest tests -q"
_FACT = "the worker page is rendered from the node"

_V5 = '''\
name = "{name}"
goal = "Foreman starts a front from the owner's inputs alone."
finish-line = "The front is admitted, recorded and old briefs still add."
decisions = [
  "Everything is a CLI verb.",
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


def _bare(world) -> tuple[Path, Path]:
    stored = world.get("_34_bare")
    if stored is not None:
        return Path(stored), Path(world["_34_git"])
    root = Path(world["repo_a"]).parent / "m3-wp"
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
    _git(bare, "config", "user.email", "proof@example.invalid")
    _git(bare, "config", "user.name", "proof")
    world["_34_bare"] = str(bare)
    world["_34_git"] = str(src)
    return bare, src


def _add_front(world, name: str, url: str, work: str) -> None:
    directory = Path(world["specs"]) / f"m3-wp-{name}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.toml").write_text(
        _V5.format(name=name, url=url, work=work), encoding="utf-8")
    added = run_foreman(["front", "add", str(directory)])
    if added.returncode != 0:
        raise Failure(f"front add {name} failed: {added.stderr[-800:]}")


def _ensure_work(src: Path, bare: Path, work: str) -> None:
    proc = _git(src, "branch", work, "main")
    if proc.returncode != 0 and "already exists" not in (proc.stderr or ""):
        raise Failure(f"git branch {work} failed: {proc.stderr[-400:]}")
    proc = subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", "-q", "origin",
         f"+refs/heads/{work}:refs/remotes/origin/{work}"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"fetch origin {work} failed: {proc.stderr[-400:]}")


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
        "--verify", flags.get("verify", _VERIFY),
        "--must-not-touch", flags.get("must_not_touch", _MUST),
        "--reason", flags.get("reason", "the tree needs this node"),
        "--break", "admit a job as a parent",
        "--repo", "foreman",
    ]
    if flags.get("role") is not None:
        argv.extend(["--role", flags["role"]])
    if flags.get("node_id") is not None:
        argv.extend(["--id", flags["node_id"]])
    if flags.get("what") is not None:
        argv.extend(["--what", flags["what"]])
    proc = run_foreman(argv, session=sid)
    if proc.returncode != 0:
        raise Failure(
            f"node add {kind} {title!r} failed: {proc.stderr[-800:]}")
    return proc.stdout.strip()


def _job_file(front: str, node_id: str) -> Path:
    state = Path(os.environ["FOREMAN_STATE"])
    path = state / "fronts" / front / "jobs.jsonl"
    latest = None
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                latest = row
    if not isinstance(latest, dict) or not latest.get("worktree"):
        raise Failure(f"no started job worktree for {node_id}")
    job = Path(latest["worktree"]) / "FOREMAN-JOB.md"
    if not job.is_file():
        raise Failure(f"FOREMAN-JOB.md missing at {job}")
    return job


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure(f"BREAK apply: needle not in {_SRC.name}")
    world["_34_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_34_src", None)
    if saved is None:
        return
    _SRC.write_text(saved, encoding="utf-8")
    _drop_pyc(_SRC)


BREAK = (
    "omit the node's what from the rendered worker page",
    apply, restore,
)


def run(world) -> str:
    assert_world()
    bare, src = _bare(world)
    n = world.get("_34_n", 0) + 1
    world["_34_n"] = n
    name = f"wp{n}"
    work = f"w{name}"
    _add_front(world, name, str(bare), work)
    _ensure_work(src, bare, work)
    sid = _register(name)
    fact_id = "fct-page01"
    what = f"Build the page. Names {fact_id}."
    map_path = Path(os.environ["FOREMAN_STATE"]) / "fronts" / name / "map.jsonl"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    with map_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "id": fact_id, "front": name, "repo": "foreman",
            "section": "Launch", "text": _FACT, "basis": "seen",
            "refs": [], "seen_at": "2026-09-10T00:00:00+00:00",
            "seen_where": "src/foreman/launch.py", "commit": "abc1234",
            "by": sid,
        }) + "\n")
    mil = _add_ok(name, sid, name, "milestone", "milestone one",
                  node_id=f"mil-wp{n}")
    tsk = _add_ok(name, sid, mil, "task", "the door",
                  node_id=f"tsk-wp{n}")
    job = _add_ok(name, sid, tsk, "job", "implement the door",
                  role="builder", node_id=f"job-wpa{n}",
                  what=what, must_not_touch=_MUST, verify=_VERIFY,
                  reason=f"Decision 7 cites {fact_id}.")
    queued = run_foreman(["job", "queue", name, job], session=sid)
    if queued.returncode != 0:
        raise Failure(f"job queue failed: {queued.stderr[-800:]}")
    tick = run_foreman(["collector", "once"])
    if tick.returncode != 0:
        raise Failure(f"collector once failed: {tick.stderr[-800:]}")
    page = _job_file(name, job).read_text(encoding="utf-8")
    missing = []
    if what not in page:
        missing.append("what")
    if f"Must not touch: {_MUST}" not in page:
        missing.append("must-not-touch")
    if _VERIFY not in page:
        missing.append("verify")
    if f"- branch: job/{name}-{job}" not in page:
        missing.append("branch")
    if fact_id not in page or _FACT not in page:
        missing.append("map fact")
    if missing:
        raise Failure(
            f"FOREMAN-JOB.md missing {missing}:\n{page[-2000:]}")
    script = _add_ok(name, sid, tsk, "job", "a script item",
                     role="script", node_id=f"job-wps{n}")
    refused = run_foreman(["job", "queue", name, script], session=sid)
    if refused.returncode == 0:
        raise Failure("job queue of a sheetless role was admitted")
    err = refused.stderr or ""
    if "--sheet-replace" not in err:
        raise Failure(
            f"sheetless queue did not name --sheet-replace: {err[-800:]}")
    return (
        f"FOREMAN-JOB.md has what, must-not-touch, verify, "
        f"branch job/{name}-{job} and {fact_id}; "
        f"script queue named --sheet-replace"
    )
