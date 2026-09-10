"""Clause 5.3: pool reset, old worktree clean, nightly once, upgrade waits."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAUSE = ("5.3", "clockwork")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "clockwork.py"
_NEEDLE = "                or moment < until:\n"
_PATCH = "                or True:\n"

_SPEC = (
    "WHAT: print hello and stop.\n"
    "INPUTS: none.\n"
    "OUTPUTS: the word hello.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: test -f FOREMAN-JOB.md\n"
)


def _drop_pyc(path: Path) -> None:
    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return
    for item in cache.iterdir():
        if item.name.startswith(path.stem + "."):
            item.unlink(missing_ok=True)


def apply(world) -> None:
    text = _SRC.read_text(encoding="utf-8")
    if _NEEDLE not in text:
        raise Failure("BREAK apply: _pool_reset until-guard moved")
    world["_53_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_53_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = (
    "_pool_reset treats every out_until as still in the future",
    apply, restore,
)


def _state() -> Path:
    return Path(os.environ["FOREMAN_STATE"])


def _config() -> Path:
    return Path(os.environ["FOREMAN_CONFIG"])


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
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


def _clk(item: str) -> list[dict]:
    return [row for row in _rows(_state() / "clockwork.jsonl")
            if row.get("item") == item]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True)


def _retire_queued() -> None:
    root = _state() / "fronts"
    if not root.is_dir():
        return
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        path = directory / "front.jsonl"
        rows = _rows(path)
        if not rows:
            continue
        last = dict(rows[-1])
        if last.get("state") in ("queued", "active"):
            last["state"] = "done"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(last) + "\n")
    path = _state() / "reservations.jsonl"
    folded = _fold(_rows(path))
    extra = [dict(rec, released_at="2026-09-10T12:00:00+00:00")
             for rec in folded.values() if not rec.get("released_at")]
    if extra:
        with path.open("a", encoding="utf-8") as handle:
            for rec in extra:
                handle.write(json.dumps(rec) + "\n")


def _clear_clockwork_state() -> None:
    path = _state() / "collector.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    cw = data.get("clockwork")
    if not isinstance(cw, dict):
        return
    for key in ("last_run", "running"):
        table = cw.get(key)
        if isinstance(table, dict):
            table.pop("nightly", None)
            table.pop("upgrade", None)
    cw.pop("upgrade_waits", None)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def _ensure_clockwork_config() -> None:
    path = _config() / "foreman.toml"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if "[clockwork]" in text:
        return
    path.write_text(
        text + "\n[clockwork]\n"
        'nightly = "echo from-nightly"\n'
        'nightly_every = "24h"\n'
        'upgrade = "echo from-upgrade"\n'
        'upgrade_every = "24h"\n',
        encoding="utf-8")


def _seed_session(entry: dict) -> None:
    path = _state() / "roster.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() \
            else {"sessions": {}}
    except (OSError, json.JSONDecodeError, ValueError):
        data = {"sessions": {}}
    if not isinstance(data, dict):
        data = {"sessions": {}}
    sessions = data.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
        data["sessions"] = sessions
    sessions[entry["id"]] = entry
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def _make_worktree(root: Path, sid: str) -> Path:
    repo = root / "src"
    wt = root / "wt-old"
    repo.mkdir(parents=True)
    for argv in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "proof@example.invalid"),
                 ("config", "user.name", "proof"),
                 ("commit", "-q", "--allow-empty", "-m", "init")):
        proc = _git(repo, *argv)
        if proc.returncode != 0:
            raise Failure(
                f"git {' '.join(argv)} failed: {(proc.stderr or '')[-400:]}")
    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b",
         f"job/{sid}", str(wt)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"worktree add failed: {(proc.stderr or '')[-400:]}")
    return wt


def run(world) -> str:
    assert_world()
    n = world.get("_53_n", 0) + 1
    world["_53_n"] = n
    _retire_queued()
    _clear_clockwork_state()
    _ensure_clockwork_config()
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    old_exit = (now - timedelta(hours=25)).isoformat()

    with (_state() / "pools.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "id": "grok", "pool": "grok", "out_until": past,
            "because": "quota",
        }) + "\n")

    sid = f"ses-old{n}"
    root = Path(world["repo_a"]).parent / f"m5-clk{n}"
    wt = _make_worktree(root, sid)
    _seed_session({
        "id": sid, "role": "grok", "pool": "grok", "front": "f",
        "job": None, "pid": None, "worktree": str(wt),
        "branch": f"job/{sid}", "state": "exited",
        "started_at": (now - timedelta(days=3)).isoformat(),
        "exited_at": old_exit,
    })

    front = f"clk{n}"
    jobs = _state() / "fronts" / front / "jobs.jsonl"
    jobs.parent.mkdir(parents=True, exist_ok=True)
    with jobs.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "job-run1", "state": "running"}) + "\n")

    before_nightly = len(_clk("nightly"))
    before_upgrade = len(_clk("upgrade"))
    tick = run_foreman(["collector", "once"])
    if tick.returncode != 0:
        raise Failure(f"collector once failed: {tick.stderr[-800:]}")

    pools = _fold(_rows(_state() / "pools.jsonl"))
    grok = pools.get("grok") or {}
    if grok.get("out_until") is not None:
        raise Failure(f"pool grok still out: {grok}")
    if grok.get("because") != "reset reached":
        raise Failure(f"reset line because is {grok.get('because')!r}")

    spec = Path(world["specs"]) / f"clk-hello-{n}.md"
    spec.write_text(_SPEC, encoding="utf-8")
    admitted = run_foreman([
        "launch", "grok", "grok", str(spec),
        "--repo", str(world["repo_a"]),
        "--worktree", str(Path(world["repo_a"]).parent / f"wt-clk-{n}"),
        "--dry-run",
    ])
    if admitted.returncode != 0:
        raise Failure("launch on the reset pool was refused")

    if wt.exists():
        raise Failure(f"old worktree still present: {wt}")
    roster = json.loads((_state() / "roster.json").read_text(encoding="utf-8"))
    sessions = roster.get("sessions") if isinstance(roster, dict) else {}
    entry = (sessions or {}).get(sid) or {}
    if entry.get("state") != "history":
        raise Failure(f"old session state is {entry.get('state')!r}")

    if "upgrade waits: job job-run1 running on " + front not in tick.stdout:
        raise Failure(
            f"tick never said upgrade waits:\n{tick.stdout[-1500:]}")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        nightly = _clk("nightly")
        if len(nightly) > before_nightly:
            break
        time.sleep(0.05)
        again = run_foreman(["collector", "once"])
        if again.returncode != 0:
            raise Failure(f"collector once failed: {again.stderr[-800:]}")
    nightly = _clk("nightly")
    if len(nightly) != before_nightly + 1:
        raise Failure(
            f"nightly ran {len(nightly) - before_nightly} times, not once")
    rec = nightly[-1]
    if rec.get("exit") != 0:
        raise Failure(f"nightly exit is {rec.get('exit')!r}")
    ref = rec.get("output_ref") or ""
    if not ref or Path(ref).is_absolute():
        raise Failure(f"nightly output_ref is {ref!r}")
    body = (_state() / ref).read_text(encoding="utf-8")
    if "from-nightly" not in body:
        raise Failure(f"nightly output was {body!r}")

    later = run_foreman(["collector", "once"])
    if later.returncode != 0:
        raise Failure(f"later collector once failed: {later.stderr[-800:]}")
    if len(_clk("nightly")) != before_nightly + 1:
        raise Failure("nightly ran again on the next tick")

    with jobs.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "job-run1", "state": "returned"}) + "\n")
    last = run_foreman(["collector", "once"])
    if last.returncode != 0:
        raise Failure(f"upgrade tick failed: {last.stderr[-800:]}")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len(_clk("upgrade")) > before_upgrade:
            break
        time.sleep(0.05)
        last = run_foreman(["collector", "once"])
        if last.returncode != 0:
            raise Failure(f"upgrade tick failed: {last.stderr[-800:]}")
    if len(_clk("upgrade")) != before_upgrade + 1:
        raise Failure("upgrade did not run after the job returned")
    return (
        f"tick reset grok (out_until null) and a launch was admitted; "
        f"removed {sid}'s worktree; nightly ran once with exit 0 and not "
        f"again; upgrade waited on job-run1 then ran"
    )
