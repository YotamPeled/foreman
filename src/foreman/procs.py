"""Process-table reads for the collector. Standard library only, no psutil.

Everything here reads ``/proc`` and falls back cleanly where there is
none: unknown pids read as dead-or-zero, never as an exception. The one
fact the collector needs about how the launcher starts a worker is
centralised here: the recorded pid is a shell that burns no cpu itself,
so cpu is always summed over the whole tree below it.
"""

from __future__ import annotations

import os
import signal

PROC_ROOT = "/proc"


def _clock_tick() -> float:
    try:
        return float(os.sysconf(os.sysconf_names["SC_CLK_TCK"]))
    except (KeyError, ValueError, OSError, AttributeError):
        return 100.0


def _read_stat(pid: int) -> tuple[int, str, float, int] | None:
    """(ppid, state, cpu seconds incl. reaped children, starttime) or None."""
    try:
        with open(f"{PROC_ROOT}/{pid}/stat", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    try:
        _, _, rest = text.rpartition(")")
        parts = rest.split()
        state = parts[0]
        ppid = int(parts[1])
        ticks = float(parts[11]) + float(parts[12]) + float(parts[13]) + float(parts[14])
        starttime = int(parts[19])
        return ppid, state, ticks / _clock_tick(), starttime
    except (ValueError, IndexError):
        return None


def proc_starttime(pid: int | None) -> int | None:
    """Starttime (clock ticks since boot) for ``pid``, or None when unreadable."""
    if pid is None or pid <= 0:
        return None
    info = _read_stat(pid)
    return info[3] if info is not None else None


def same_process(pid: int | None, stored: int | None,
                 table: dict[int, dict] | None = None) -> bool:
    """True when ``pid`` is still the process recorded beside ``stored``.

    ``stored=None`` is a legacy record without identity: it is trusted so
    older rosters keep working. Otherwise the current starttime must equal
    the stored one; an unreadable pid or a mismatch reads as "not the
    same" (pid reuse), never as alive.
    """
    if pid is None or pid <= 0:
        return False
    if stored is None:
        return True
    current: int | None = None
    if table is not None and pid in table:
        current = table[pid].get("starttime")
    if current is None:
        current = proc_starttime(pid)
    return current is not None and current == stored


def _read_cmdline(pid: int) -> str:
    try:
        with open(f"{PROC_ROOT}/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def pid_alive(pid: int | None) -> bool:
    """True only for a living, unreaped process. Zombies read as dead."""
    if pid is None or pid <= 0:
        return False
    info = _read_stat(pid)
    if info is not None:
        return info[1] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def snapshot() -> dict[int, dict]:
    """One pass over the process table: pid -> ppid, state, cpu, cmdline.

    Empty on a system without /proc; callers treat that as "nothing
    seen" and fall back to signal probing per pid.
    """
    table: dict[int, dict] = {}
    try:
        entries = os.listdir(PROC_ROOT)
    except OSError:
        return table
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        info = _read_stat(pid)
        if info is None:
            continue
        ppid, state, cpu, starttime = info
        table[pid] = {
            "ppid": ppid,
            "state": state,
            "cpu_s": cpu,
            "starttime": starttime,
            "cmdline": _read_cmdline(pid),
        }
    return table


def descendants(root: int, table: dict[int, dict] | None = None) -> set[int]:
    """Every live pid below ``root``, root included when it is present.

    Zombies are reaped-but-unwaited dead: they are excluded, so a dead
    process never looks alive forever behind its zombie entry.
    """
    if table is None:
        table = snapshot()
    children: dict[int, list[int]] = {}
    for pid, info in table.items():
        if info.get("state") == "Z":
            continue
        children.setdefault(info["ppid"], []).append(pid)
    if table.get(root, {}).get("state") == "Z":
        return set()
    seen = {root}
    found = {root} if root in table else set()
    stack = [root]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in seen:
                seen.add(child)
                found.add(child)
                stack.append(child)
    return found


def tree_cpu_seconds(pid: int | None,
                     table: dict[int, dict] | None = None) -> float:
    """Cpu over the whole tree below ``pid``, not the shell's own.

    The launcher records the wrapper shell's pid; that shell only waits
    on its pipeline, so its own utime/stime stay near zero while the
    vendor child burns seconds. Summing every process below it (each
    entry already including its own reaped children) is what keeps a
    healthy job from looking idle.
    """
    if pid is None or pid <= 0:
        return 0.0
    if table is None:
        table = snapshot()
    if not table:
        info = _read_stat(pid)
        return info[2] if info is not None else 0.0
    return sum(table[member]["cpu_s"] for member in descendants(pid, table))


def working_directory(pid: int) -> str | None:
    """Where a process is working, or None when it cannot be read.

    A process whose cwd cannot be read is somebody else's: the swarm
    only ever asks about processes it might own.
    """
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def executable_of(cmdline: str) -> str:
    """Basename of the executable: the first cmdline token's final component."""
    first = cmdline.split()[0] if cmdline.split() else ""
    return first.rsplit("/", 1)[-1]


def kill_tree(pid: int | None,
              table: dict[int, dict] | None = None) -> list[int]:
    """SIGKILL every live process in the tree below ``pid``, leaves first.

    Returns the pids signalled. Never raises for a pid that is already
    gone or owned by someone else.
    """
    if pid is None or pid <= 0:
        return []
    if table is None:
        table = snapshot()
    members = descendants(pid, table) if table else (
        {pid} if pid_alive(pid) else set()
    )
    depth: dict[int, int] = {}

    def _depth(member: int) -> int:
        trail, current = 0, member
        while current != pid and current in table and trail <= len(table) + 1:
            current = table[current]["ppid"]
            trail += 1
        return trail

    if table:
        for member in members:
            depth[member] = _depth(member)
    signalled = []
    for member in sorted(members, key=lambda m: -depth.get(m, 0)):
        try:
            os.kill(member, signal.SIGKILL)
            signalled.append(member)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return signalled


def kill_job(pid: int | None, pgid: int | None = None) -> tuple[list[int], set[int]]:
    """Kill a job through its process group, then confirm it is gone.

    Signals the launcher-recorded process group first (so children spawned
    or reparented since any earlier snapshot still die), then re-reads the
    tree and kills stragglers leaves-first. Returns (signalled, remaining):
    ``remaining`` is the live tree after the kill, empty when the job is
    actually gone. Never raises for gone or foreign pids.
    """
    if pid is None or pid <= 0:
        return [], set()
    signalled: list[int] = []
    if pgid is not None and pgid > 0:
        try:
            os.killpg(pgid, signal.SIGKILL)
            signalled.append(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    table = snapshot()
    members = descendants(pid, table) if table else (
        {pid} if pid_alive(pid) else set()
    )
    depth: dict[int, int] = {}

    def _depth(member: int) -> int:
        trail, current = 0, member
        while current != pid and current in table and trail <= len(table) + 1:
            current = table[current]["ppid"]
            trail += 1
        return trail

    if table:
        for member in members:
            depth[member] = _depth(member)
    for member in sorted(members, key=lambda m: -depth.get(m, 0)):
        if member in signalled:
            continue
        try:
            os.kill(member, signal.SIGKILL)
            signalled.append(member)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    fresh = snapshot()
    if fresh:
        remaining = descendants(pid, fresh)
    else:
        remaining = {pid} if pid_alive(pid) else set()
    return signalled, remaining
