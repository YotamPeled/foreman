"""Clause 1.8: a windowed summon leaves a seeded checkpoint at mint."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

CLAUSE = ("1.8", "seeded checkpoint")


def _field(out: str, prefix: str) -> str:
    needle = prefix if prefix.endswith(" ") else prefix + " "
    for line in out.splitlines():
        if line.startswith(needle):
            return line[len(needle):].strip()
    raise Failure(f"no {prefix!r} line in output:\n{out[-2000:]}")


def _launcher_path(world) -> Path:
    return Path(world["bin"]) / "fake-window-m18"


def _write_good_launcher(world) -> Path:
    state = Path(os.environ["FOREMAN_STATE"])
    path = _launcher_path(world)
    path.write_text(
        "#!/bin/sh\n"
        'sid=${1#foreman-}\n'
        f'test -f {json.dumps(str(state / "sessions"))}/$sid/checkpoint.json '
        '|| { echo "checkpoint.json missing at process start" >&2; exit 1; }\n'
        "shift\n"
        'exec "$@"\n',
        encoding="utf-8")
    path.chmod(0o755)
    return path


def _extra(world) -> dict:
    home = Path(world["repo_a"]).parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "FOREMAN_VENDOR_CONFIG": str(home / "vendor.json"),
        "FOREMAN_WINDOW_LAUNCHER": str(_launcher_path(world)),
    }


def _kill_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def apply(world) -> None:
    state = Path(os.environ["FOREMAN_STATE"])
    path = _launcher_path(world)
    path.write_text(
        "#!/bin/sh\n"
        'sid=${1#foreman-}\n'
        f'echo $$ > {json.dumps(str(state / "sessions"))}/$sid/pid\n'
        "exit 1\n",
        encoding="utf-8")
    path.chmod(0o755)


def restore(world) -> None:
    _write_good_launcher(world)


BREAK = ("replace the window launcher so it dies before the session is confirmed",
         apply, restore)


def run(world) -> str:
    assert_world()
    if not _launcher_path(world).exists():
        _write_good_launcher(world)
    proc = run_foreman(
        ["launch", "supervisor", "alpha",
         "--repo", str(world["repo_a"]),
         "--workspace", "6"],
        extra_env=_extra(world),
        timeout=60)
    sid = None
    try:
        if proc.returncode != 0:
            raise Failure(
                f"windowed supervisor launch exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[-800:]}")
        sid = _field(proc.stdout, "session:")
        state = Path(os.environ["FOREMAN_STATE"])
        path = state / "sessions" / sid / "checkpoint.json"
        if not path.is_file():
            raise Failure(f"no checkpoint.json at {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("seeded") is not True:
            raise Failure(f"checkpoint seeded is {payload.get('seeded')!r}")
        roster = json.loads(
            (state / "roster.json").read_text(encoding="utf-8"))
        pid = ((roster.get("sessions") or {}).get(sid) or {}).get("pid")
        _kill_pid(pid)
        return f"session {sid} checkpoint seeded==true"
    except Exception:
        if sid:
            try:
                roster = json.loads(
                    (Path(os.environ["FOREMAN_STATE"]) / "roster.json")
                    .read_text(encoding="utf-8"))
                _kill_pid(((roster.get("sessions") or {}).get(sid) or {}).get("pid"))
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        raise
