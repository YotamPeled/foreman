"""Clause 5.2: two queued fronts; one tick starts the top only."""

from __future__ import annotations

import json
import os
from pathlib import Path

CLAUSE = ("5.2", "front_queue")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "collector.py"
_NEEDLE = (
    "    session, _err = launch_module.start_queued_front(\n"
    "        name, by=COLLECTOR_SUBJECT, now=now_iso)\n"
)
_PATCH = (
    "    session, _err = (None, \"broken\") or launch_module.start_queued_front(\n"
    "        name, by=COLLECTOR_SUBJECT, now=now_iso)\n"
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
        raise Failure("BREAK apply: _front_queue_tick start call moved")
    world["_52_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_52_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = (
    "the queue tick never calls start_queued_front so the top front stays queued",
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


def _front(name: str) -> dict:
    rows = _rows(_state() / "fronts" / name / "front.jsonl")
    if not rows:
        raise Failure(f"no front record for {name}")
    return rows[-1]


def _open_for(front: str) -> list[dict]:
    folded = _fold(_rows(_state() / "reservations.jsonl"))
    return [rec for rec in folded.values()
            if rec.get("front") == front and not rec.get("released_at")]


def _ok(argv: list[str]) -> str:
    proc = run_foreman(argv)
    if proc.returncode != 0:
        raise Failure(
            f"foreman {' '.join(argv[:4])} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[-800:]}")
    return proc.stdout


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


def _write_v5(name: str, builders: int, requested_at: str) -> None:
    directory = _state() / "fronts" / name
    directory.mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "fake", "pool": "fake", "model": "fake-test-model",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "fake", "pool": "fake", "model": "fake-test-model",
         "effort": "high", "count": builders, "role": "builder"},
    ]
    line = {
        "id": f"frt-{name}", "name": name, "state": "queued", "shape": "v5",
        "prefer": 0, "requested_at": requested_at,
        "goal": "Run in queue order.",
        "finish_line": "The top front starts.",
        "allocation": {"muse": builders}, "team": team,
        "supervisor": {"agent": "fake", "pool": "fake",
                       "model": "fake-test-model", "effort": "high"},
    }
    with (directory / "front.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _supervisor_spawns(world) -> list[dict]:
    rows = []
    for line in Path(world["supervisor_log"]).read_text(
            encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def run(world) -> str:
    assert_world()
    n = world.get("_52_n", 0) + 1
    world["_52_n"] = n
    top, nxt = f"qt{n}", f"qn{n}"
    _retire_queued()
    capped = run_foreman(["cap", "fake", "3"])
    if capped.returncode != 0:
        raise Failure(f"cap fake 3 failed: {capped.stderr[-800:]}")
    _write_v5(top, 2, "2026-09-10T12:00:00+00:00")
    _write_v5(nxt, 2, "2026-09-10T12:00:01+00:00")

    queued = _ok(["front", "queue"])
    if queued.splitlines() != [
            f"{top}  team fits",
            f"{nxt}  behind {top}"]:
        raise Failure(f"front queue printed:\n{queued}")

    Path(world["supervisor_log"]).write_text("", encoding="utf-8")
    tick = run_foreman(["collector", "once"])
    if tick.returncode != 0:
        raise Failure(f"collector once failed: {tick.stderr[-800:]}")
    spawns = _supervisor_spawns(world)
    if [row.get("front") for row in spawns] != [top]:
        raise Failure(f"first tick spawned {spawns}, not only {top}")
    if (spawns[0].get("model"), spawns[0].get("effort")) != (
            "fake-test-model", "high"):
        raise Failure(f"supervisor spawn was {spawns[0]}")
    recs = _open_for(top)
    if len(recs) != 1 or recs[0].get("phase") != "builders" \
            or recs[0].get("count") != 2 or recs[0].get("pool") != "fake":
        raise Failure(f"top reservation was {recs}")
    if _open_for(nxt):
        raise Failure(f"second front reserved {_open_for(nxt)}")
    if _front(top).get("state") != "active":
        raise Failure(f"top state is {_front(top).get('state')!r}, not active")
    if _front(nxt).get("state") != "queued":
        raise Failure(
            f"second state is {_front(nxt).get('state')!r}, not queued")

    stopped = _ok(["front", "stop", top, "--reason", "owner paused this front"])
    if f"{top} stopped" not in stopped:
        raise Failure(f"front stop printed {stopped!r}")
    if _front(top).get("state") != "stopped":
        raise Failure(f"stopped front is {_front(top).get('state')!r}")
    if _open_for(top):
        raise Failure("stop did not release the reservation")

    tick2 = run_foreman(["collector", "once"])
    if tick2.returncode != 0:
        raise Failure(f"second collector once failed: {tick2.stderr[-800:]}")
    spawns = _supervisor_spawns(world)
    if [row.get("front") for row in spawns] != [top, nxt]:
        raise Failure(f"after stop the spawns were {spawns}")
    if _front(nxt).get("state") != "active":
        raise Failure(
            f"second front is {_front(nxt).get('state')!r}, not active")
    recs = _open_for(nxt)
    if len(recs) != 1 or recs[0].get("count") != 2:
        raise Failure(f"second reservation was {recs}")

    resumed = _ok(["front", "resume", top])
    if f"{top} queued" not in resumed:
        raise Failure(f"front resume printed {resumed!r}")
    if _front(top).get("state") != "queued":
        raise Failure(f"resumed front is {_front(top).get('state')!r}")
    again = _ok(["front", "queue"])
    if again.splitlines() != [
            f"{top}  pool fake: cap 3, reserved 2, wants 2"]:
        raise Failure(f"front queue after resume printed:\n{again}")
    return (
        f"front queue printed team fits then behind; one tick started "
        f"{top} (supervisor spawn, builders 2, active); stop released "
        f"and the next tick started {nxt}; resume re-queued {top}"
    )
