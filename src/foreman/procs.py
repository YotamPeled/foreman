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


def _read_stat(pid: int) -> tuple[int, str, float] | None:
    """(ppid, state, cpu seconds incl. reaped children) or None when unreadable."""
    try:
        with open(f"{PROC_ROOT}/{pid}/stat", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    try:
        _, _, rest = text.rpartition(")")
        parts = rest.split()
        ppid = int(parts[1])
        state = parts[2]
        ticks = float(parts[11]) + float(parts[12]) + float(parts[13]) + float(parts[14])
        return ppid, state, ticks / _clock_tick()
    except (ValueError, IndexError):
        return None


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
        ppid, state, cpu = info
        table[pid] = {
            "ppid": ppid,
            "state": state,
            "cpu_s": cpu,
            "cmdline": _read_cmdline(pid),
        }
    return table


def descendants(root: int, table: dict[int, dict] | None = None) -> set[int]:
    """Every live pid below ``root``, root included when it is present."""
    if table is None:
        table = snapshot()
    children: dict[int, list[int]] = {}
    for pid, info in table.items():
        children.setdefault(info["ppid"], []).append(pid)
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
