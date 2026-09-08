"""Mint short, roughly time-ordered ids: <kind>-<seven base32 chars>.

The current time in milliseconds occupies the high bits so ids sort
roughly by age; the low bits are fresh randomness per tick, incremented
while the clock stands still, so rapid mints stay unique and ordered and
cross-process collisions vanish.
"""

from __future__ import annotations

import secrets
import threading
import time

_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
_TIME_MASK = (1 << 25) - 1
_RAND_BITS = 10
_RAND_MAX = (1 << _RAND_BITS) - 1

KIND_PREFIXES = {
    "component": "cmp",
    "task": "tas",
    "job": "job",
    "merge": "mrg",
    "session": "ses",
    "pool": "pol",
    "allocation": "alc",
    "slot": "slt",
    "ruling": "rul",
    "evidence": "evi",
    "finding": "fnd",
    "measurement": "msm",
    "checkpoint": "chk",
    "inbox": "inb",
    "anomaly": "anm",
    "event": "evt",
}

_lock = threading.Lock()
_last_ms = 0
_last_rand = 0


def mint(kind: str) -> str:
    prefix = KIND_PREFIXES[kind]
    now_ms = time.time_ns() // 1_000_000
    global _last_ms, _last_rand
    with _lock:
        if now_ms > _last_ms:
            _last_ms = now_ms
            _last_rand = secrets.randbelow(_RAND_MAX + 1)
        else:
            _last_rand += 1
            if _last_rand > _RAND_MAX:
                _last_ms += 1
                _last_rand = 0
        value = ((_last_ms & _TIME_MASK) << _RAND_BITS) | _last_rand
    chars = []
    for _ in range(7):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return f"{prefix}-{''.join(reversed(chars))}"
