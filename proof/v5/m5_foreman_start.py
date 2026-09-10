"""Clause 5.1: start writes the unit, enables it and spawns the foreman."""

from __future__ import annotations

import json
import os
from pathlib import Path

CLAUSE = ("5.1", "foreman_start")

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "foreman" / "start.py"
_NEEDLE = (
    "    live = launch_module.live_foreman()\n"
    "    if live:\n"
    "        violations.append(f\"a foreman is live: {live[0][0]}; stop it first\")\n"
)
_PATCH = (
    "    live = launch_module.live_foreman()\n"
    "    if False and live:\n"
    "        violations.append(f\"a foreman is live: {live[0][0]}; stop it first\")\n"
)

UNIT = "foreman-collector.service"
MODEL = "claude-fable-5-1"
EFFORT = "xhigh"


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
        raise Failure("BREAK apply: start_main live-foreman guard moved")
    world["_51_src"] = text
    _SRC.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    _drop_pyc(_SRC)


def restore(world) -> None:
    saved = world.pop("_51_src", None)
    if saved is not None:
        _SRC.write_text(saved, encoding="utf-8")
        _drop_pyc(_SRC)


BREAK = (
    "start_main skips the live-foreman refusal so a second start is not named",
    apply, restore,
)


def _state() -> Path:
    return Path(os.environ["FOREMAN_STATE"])


def _config() -> Path:
    return Path(os.environ["FOREMAN_CONFIG"])


def _unit_path() -> Path:
    return _config() / "systemd" / "user" / UNIT


def _roster() -> dict:
    path = _state() / "roster.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return sessions if isinstance(sessions, dict) else {}


def _reset(world) -> None:
    """A BREAK re-run shares the world: drop the unit, the door state
    and any live foreman the previous pass spawned."""
    Path(world["systemctl_log"]).write_text("", encoding="utf-8")
    Path(world["systemctl_state"]).write_text(
        '{"active": false}\n', encoding="utf-8")
    Path(world["spawn_wait_log"]).write_text("", encoding="utf-8")
    _unit_path().unlink(missing_ok=True)
    path = _state() / "roster.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return
    changed = False
    for entry in sessions.values():
        if isinstance(entry, dict) and entry.get("role") == "foreman":
            if entry.get("state") in ("starting", "running"):
                entry["state"] = "exited"
                changed = True
    if changed:
        path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def _changes(log: str) -> list[str]:
    out = []
    for line in log.splitlines():
        if not line.strip() or line.startswith("is-active"):
            continue
        out.append(line.strip())
    return out


def run(world) -> str:
    assert_world()
    _reset(world)
    repo = str(world["repo_a"])
    argv = ["start", "--model", MODEL, "--effort", EFFORT, "--repo", repo]

    doctor0 = run_foreman(["doctor"])
    if f"doctor: collector unit {UNIT}: absent" not in doctor0.stdout:
        raise Failure(
            f"doctor before start never named the absent unit:\n"
            f"{doctor0.stdout[-1500:]}")
    if "doctor: no foreman" not in doctor0.stdout:
        raise Failure(
            f"doctor before start never said no foreman:\n"
            f"{doctor0.stdout[-1500:]}")

    dry = run_foreman([*argv, "--dry-run"])
    if dry.returncode != 0:
        raise Failure(
            f"start --dry-run failed: {(dry.stderr or dry.stdout)[-800:]}")
    dry_lines = [ln for ln in dry.stdout.splitlines() if ln.strip()]
    if len(dry_lines) != 2 or not all(ln.startswith("would ") for ln in dry_lines):
        raise Failure(f"dry-run did not print two would-lines:\n{dry.stdout}")
    if f"--model {MODEL} --effort {EFFORT}" not in dry.stdout:
        raise Failure(f"dry-run never named the model and effort:\n{dry.stdout}")
    if _unit_path().exists():
        raise Failure("dry-run wrote the collector unit")
    if _changes(Path(world["systemctl_log"]).read_text(encoding="utf-8")):
        raise Failure("dry-run called systemd")
    if Path(world["spawn_wait_log"]).read_text(encoding="utf-8").strip():
        raise Failure("dry-run spawned the foreman")
    if any(e.get("role") == "foreman" for e in _roster().values()
           if isinstance(e, dict)):
        raise Failure("dry-run rostered a foreman")

    started = run_foreman(argv)
    if started.returncode != 0:
        raise Failure(
            f"start failed (exit {started.returncode}): "
            f"{(started.stderr or started.stdout)[-800:]}")
    unit = _unit_path()
    if not unit.is_file():
        raise Failure("start did not write the collector unit")
    body = unit.read_text(encoding="utf-8")
    if str(_state()) not in body or str(_config()) not in body:
        raise Failure(f"unit does not name this world's paths:\n{body[-800:]}")
    sys_log = Path(world["systemctl_log"]).read_text(encoding="utf-8")
    if _changes(sys_log) != [
            "daemon-reload",
            f"enable --now {UNIT}"]:
        raise Failure(f"systemd calls were {_changes(sys_log)!r}, not "
                      f"daemon-reload then enable --now")
    spawn_log = Path(world["spawn_wait_log"]).read_text(encoding="utf-8")
    if spawn_log.count("===SPAWN===") != 1:
        raise Failure(f"launcher was not called once:\n{spawn_log[-1500:]}")
    if f"--model {MODEL} --effort {EFFORT} " not in spawn_log \
            and f"--model {MODEL} --effort {EFFORT}" not in spawn_log:
        raise Failure(
            f"spawned script missing --model {MODEL} --effort {EFFORT}:\n"
            f"{spawn_log[-1500:]}")
    live = [(k, v) for k, v in _roster().items()
            if isinstance(v, dict) and v.get("role") == "foreman"
            and v.get("state") == "running"]
    if len(live) != 1:
        raise Failure(f"roster has {len(live)} running foremen, not one")
    sid, record = live[0]
    if (record.get("model"), record.get("effort")) != (MODEL, EFFORT):
        raise Failure(
            f"foreman rostered as {record.get('model')!r} "
            f"{record.get('effort')!r}")
    out_lines = started.stdout.splitlines()
    if f"collector: wrote {unit}, enabled and started {UNIT}" not in out_lines:
        raise Failure(f"start did not print the collector line:\n{started.stdout}")
    if f"foreman: {sid} ({MODEL}, {EFFORT})" not in out_lines:
        raise Failure(f"start did not print the foreman line:\n{started.stdout}")

    sys_after = sys_log
    spawn_after = spawn_log
    second = run_foreman(argv)
    if second.returncode == 0:
        raise Failure("a second start was admitted")
    err = second.stderr
    if f"a foreman is live: {sid}; stop it first" not in err:
        raise Failure(f"second start never named the live foreman:\n{err[-800:]}")
    if second.stdout.strip():
        raise Failure(f"second start printed on stdout: {second.stdout!r}")
    if Path(world["systemctl_log"]).read_text(encoding="utf-8") != sys_after:
        raise Failure("second start called systemd")
    if Path(world["spawn_wait_log"]).read_text(encoding="utf-8") != spawn_after:
        raise Failure("second start spawned the launcher")

    doctor1 = run_foreman(["doctor"])
    if f"doctor: collector unit {UNIT}: active" not in doctor1.stdout:
        raise Failure(
            f"doctor after start never named the active unit:\n"
            f"{doctor1.stdout[-1500:]}")
    if f"doctor: foreman: {sid} ({MODEL}, {EFFORT})" not in doctor1.stdout:
        raise Failure(
            f"doctor after start never named the foreman:\n"
            f"{doctor1.stdout[-1500:]}")
    return (
        f"start wrote the unit, enable --now {UNIT} and spawned {sid} "
        f"({MODEL}, {EFFORT}); a second start named {sid} and called "
        f"nothing; --dry-run printed would-lines; doctor named the "
        f"active unit and the foreman"
    )
