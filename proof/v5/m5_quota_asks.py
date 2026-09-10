"""Clause 5.4: one quota ask, raise to 4 starts the front, done lowers it."""

from __future__ import annotations

import json
import os
from pathlib import Path

CLAUSE = ("5.4", "quota_asks")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "fronts.py"
_NEEDLE = "    if existing.get(pool):\n        return None\n"
_PATCH = "    if True or existing.get(pool):\n        return None\n"


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
        raise Failure("BREAK apply: maybe_file_quota_ask existing-ask guard moved")
    world["_54_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_54_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = (
    "maybe_file_quota_ask returns before filing so the tick writes no ask",
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


def _front(name: str) -> dict:
    rows = _rows(_state() / "fronts" / name / "front.jsonl")
    if not rows:
        raise Failure(f"no front record for {name}")
    return rows[-1]


def _open_asks() -> list[dict]:
    return [rec for rec in _fold(_rows(_state() / "inbox.jsonl")).values()
            if not rec.get("answered_at")]


def _caps() -> list[dict]:
    return [rec for rec in _rows(_state() / "caps.jsonl")
            if isinstance(rec, dict)]


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


def _retire() -> None:
    root = _state() / "fronts"
    if root.is_dir():
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


def _write_v5(name: str, builders: int, state: str = "queued") -> None:
    directory = _state() / "fronts" / name
    directory.mkdir(parents=True, exist_ok=True)
    team = [
        {"agent": "fake", "pool": "fake", "model": "fake-test-model",
         "effort": "high", "count": 1, "role": "supervisor"},
        {"agent": "fake", "pool": "fake", "model": "fake-test-model",
         "effort": "high", "count": builders, "role": "builder"},
    ]
    line = {
        "id": f"frt-{name}", "name": name, "state": state, "shape": "v5",
        "prefer": 0, "goal": "Run in queue order.",
        "finish_line": "The top front starts.",
        "allocation": {"muse": builders}, "team": team,
        "supervisor": {"agent": "fake", "pool": "fake",
                       "model": "fake-test-model", "effort": "high"},
    }
    with (directory / "front.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _ensure_caps_max() -> None:
    path = _config() / "foreman.toml"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if "[caps]" in text:
        return
    path.write_text(text + "\n[caps]\nmax = 6\n", encoding="utf-8")


def run(world) -> str:
    assert_world()
    n = world.get("_54_n", 0) + 1
    world["_54_n"] = n
    held, name = f"hd{n}", f"qa{n}"
    _retire()
    _ensure_caps_max()
    capped = run_foreman(["cap", "fake", "3"])
    if capped.returncode != 0:
        raise Failure(f"cap fake 3 failed: {capped.stderr[-800:]}")
    _write_v5(held, 2, state="active")
    _ok(["front", "reserve", held])
    _write_v5(name, 2)

    before = len(_open_asks())
    tick = run_foreman(["collector", "once"])
    if tick.returncode != 0:
        raise Failure(f"collector once failed: {tick.stderr[-800:]}")
    asks = _open_asks()
    if len(asks) != before + 1:
        raise Failure(f"tick filed {len(asks) - before} asks, not one")
    item = asks[-1] if asks else {}
    # Folded last-wins: the new ask is the one whose id is not previously
    # answered. Prefer the one naming this front.
    named = [a for a in asks if name in str(a.get("question") or "")]
    if len(named) != 1:
        raise Failure(f"expected one ask naming {name}, got {named}")
    item = named[0]
    if item.get("kind") != "money":
        raise Failure(f"ask kind is {item.get('kind')!r}")
    question = str(item.get("question") or "")
    want_q = (
        f"front {name} waits on pool fake: cap 3, reserved 2 by {held}, "
        f"wants 2. Raise the cap to 4 until {name} ends?")
    if question != want_q:
        raise Failure(f"ask question was {question!r}")
    if item.get("recommendation") != "raise to 4":
        raise Failure(f"recommendation is {item.get('recommendation')!r}")
    if item.get("options") != ["raise to 4", "wait", f"stop {held}"]:
        raise Failure(f"options are {item.get('options')!r}")
    record = _front(name)
    ask_map = record.get("quota_ask") or {}
    if ask_map.get("fake") != item.get("id"):
        raise Failure(f"front quota_ask is {ask_map!r}")

    iid = str(item.get("id") or "")
    _ok(["answer", iid, "raise", "to", "4"])
    tick2 = run_foreman(["collector", "once"])
    if tick2.returncode != 0:
        raise Failure(f"second tick failed: {tick2.stderr[-800:]}")
    lines = _caps()
    if not lines:
        raise Failure("answering raise to 4 wrote no caps line")
    last = lines[-1]
    if last.get("id") != "fake" or last.get("cap") != 4 \
            or last.get("until_front") != name \
            or last.get("previous_cap") != 3:
        raise Failure(f"raise caps line is {last}")
    if last.get("because") != f"quota ask {iid} for front {name}":
        raise Failure(f"caps because is {last.get('because')!r}")
    if _front(name).get("state") != "active":
        raise Failure(
            f"front is {_front(name).get('state')!r} after the raise, "
            f"not active")
    recs = [r for r in _open_for(name) if r.get("phase") == "builders"]
    if len(recs) != 1 or recs[0].get("count") != 2:
        raise Failure(f"started reservation was {recs}")

    _ok(["front", "done", name])
    if _front(name).get("state") != "done":
        raise Failure(f"front done left {_front(name).get('state')!r}")
    tick3 = run_foreman(["collector", "once"])
    if tick3.returncode != 0:
        raise Failure(f"third tick failed: {tick3.stderr[-800:]}")
    lines = _caps()
    lowered = lines[-1]
    if lowered.get("id") != "fake" or lowered.get("cap") != 3 \
            or lowered.get("until_front"):
        raise Failure(f"lowering caps line is {lowered}")
    return (
        f"one tick filed ask {iid} (cap 3, reserved 2 by {held}, wants 2, "
        f"raise to 4); answering raise to 4 capped fake at 4 and started "
        f"{name}; front done lowered the cap to 3"
    )
