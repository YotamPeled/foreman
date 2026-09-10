"""Clause 6.3: a supervisor sees only its front; the foreman sees both."""

from __future__ import annotations

import json
import os
from pathlib import Path

CLAUSE = ("6.3", "front_scope")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "caller.py"
_NEEDLE = (
    "    if isinstance(front, str) and front:\n"
    "        return [front]\n"
)
_PATCH = (
    "    if isinstance(front, str) and front:\n"
    "        return None\n"
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
        raise Failure("BREAK apply: visible_fronts own-front return moved")
    world["_63_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_63_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = (
    "visible_fronts returns None for a supervisor instead of its own front",
    apply, restore,
)


def _state() -> Path:
    return Path(os.environ["FOREMAN_STATE"])


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


def _ok(argv: list[str], session: str | None = None) -> str:
    proc = run_foreman(argv, session=session)
    if proc.returncode != 0:
        raise Failure(
            f"foreman {' '.join(argv[:4])} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[-800:]}")
    return proc.stdout


def _field(out: str, prefix: str) -> str:
    needle = prefix if prefix.endswith(" ") else prefix + " "
    for line in out.splitlines():
        if line.startswith(needle):
            return line[len(needle):].strip()
    raise Failure(f"no {prefix!r} line in output:\n{out[-2000:]}")


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
        if last.get("state") == "queued":
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


def _write_v5(name: str, requested_at: str) -> None:
    directory = _state() / "fronts" / name
    directory.mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "fake", "pool": "fake", "model": "fake-test-model",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "fake", "pool": "fake", "model": "fake-test-model",
         "effort": "high", "count": 1, "role": "builder"},
    ]
    line = {
        "id": f"frt-{name}", "name": name, "state": "queued", "shape": "v5",
        "prefer": 0, "requested_at": requested_at,
        "goal": "Show the caller's own front.",
        "finish_line": "A session sees only its front.",
        "allocation": {"fake": 1}, "team": team,
        "repositories": [{"name": "foreman", "target": "main",
                          "work": name, "base": "main",
                          "url": "https://example.invalid/foreman.git"}],
        "supervisor": {"agent": "fake", "pool": "fake",
                       "model": "fake-test-model", "effort": "high"},
    }
    with (directory / "front.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _register(role: str, front: str | None = None) -> str:
    argv = ["register", "--role", role, "--pid", str(os.getpid())]
    if front is not None:
        argv.extend(["--front", front])
    return _field(_ok(argv), "session:")


def _add_milestone(front: str, sid: str, title: str) -> None:
    _ok([
        "milestone", "add", front,
        "--title", title,
        "--done-when", f"{title} is done",
        "--verify", "python -m pytest tests -q",
        "--reason", "decision 1",
        "--break", "drop the other front",
    ], session=sid)


def _refused(argv: list[str], session: str, own: str, other: str) -> str:
    proc = run_foreman(argv, session=session)
    if proc.returncode == 0:
        raise Failure(
            f"foreman {' '.join(argv)} as {own} was admitted:\n"
            f"{proc.stdout[-400:]}")
    err = proc.stderr or ""
    if "is not yours" not in err:
        raise Failure(
            f"{argv} refusal never said is not yours: {err[-800:]}")
    if own not in err or other not in err:
        raise Failure(
            f"{argv} refusal did not name {other} and {own}: {err[-800:]}")
    return err


def run(world) -> str:
    assert_world()
    n = world.get("_63_n", 0) + 1
    world["_63_n"] = n
    own, other = f"sc{n}a", f"sc{n}b"
    _retire_queued()
    _write_v5(own, "2026-09-10T12:00:00+00:00")
    _write_v5(other, "2026-09-10T12:00:01+00:00")
    sid_a = _register("supervisor", own)
    sid_b = _register("supervisor", other)
    sid_f = _register("foreman")
    _add_milestone(own, sid_a, f"{own} screen")
    _add_milestone(other, sid_b, f"{other} screen")

    _refused(["node", "list", other], sid_a, own, other)
    _refused(["front", "show", other], sid_a, own, other)

    status_a = _ok(["status"], session=sid_a)
    before_cap = status_a.split("Capacity:", 1)[0]
    if f"{own} screen" not in status_a:
        raise Failure(
            f"status as {own} never named its milestone:\n{status_a[-2000:]}")
    if f"{other} screen" in status_a:
        raise Failure(
            f"status as {own} named {other}'s milestone:\n{status_a[-2000:]}")
    if other in before_cap:
        raise Failure(
            f"status as {own} named {other} before Capacity:\n{before_cap[-800:]}")

    queued = _ok(["front", "queue"], session=sid_a)
    if own not in queued or other not in queued:
        raise Failure(
            f"front queue as {own} printed {queued!r}, not both names")

    status_f = _ok(["status"], session=sid_f)
    if f"{own} screen" not in status_f or f"{other} screen" not in status_f:
        raise Failure(
            f"status as foreman missed a milestone:\n{status_f[-2000:]}")
    listed = _ok(["node", "list", other], session=sid_f)
    shown = _ok(["front", "show", other], session=sid_f)
    if "state:" not in shown and other not in shown:
        raise Failure(f"front show {other} as foreman printed {shown!r}")
    queued_f = _ok(["front", "queue"], session=sid_f)
    if own not in queued_f or other not in queued_f:
        raise Failure(f"front queue as foreman printed {queued_f!r}")
    return (
        f"as {own}, node list {other} and front show {other} refused "
        f"is not yours naming {other} and {own}; status named "
        f"{own} screen not {other} screen; front queue named both; "
        f"foreman status, node list {other} and front show {other} "
        f"showed both ({len(listed.splitlines())} node-list lines)"
    )
