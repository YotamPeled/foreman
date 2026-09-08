"""Shared monitor helpers: alert parsing, matching, formatting.

An alert is a comparison of the measure against a number (docs/DESIGN.md
section 4.2): ``< 0.80``, ``>= 5``, ``!= 0``. It is parsed, never executed:
no ``eval`` of a string from a file anywhere in this repository.
"""

from __future__ import annotations

import re

#: The same shape ``front add`` validates (see :mod:`foreman.fronts`).
ALERT_RE = re.compile(
    r"^\s*(<=|>=|==|!=|<|>)\s*[+-]?"
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*$")

_OPERATOR_RE = re.compile(
    r"^\s*(<=|>=|==|!=|<|>)\s*([+-]?"
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*$")


def parse_alert(alert: str | None) -> tuple[str, float] | None:
    """The (operator, threshold) of an alert string, or None.

    None means the string does not parse: the caller refuses it (at
    ``front add``) or ignores it (at tick time, where a file already on
    the ledger must never take the daemon down).
    """
    if not isinstance(alert, str):
        return None
    match = _OPERATOR_RE.match(alert)
    if match is None:
        return None
    try:
        return match.group(1), float(match.group(2))
    except ValueError:
        return None


def evaluate_alert(value: float, alert: str | None) -> bool:
    """True when the alert expression holds of ``value``.

    ``value`` is already the compared quantity: the measurement's
    value, or its value/of ratio where a denominator is known. An
    unparsable alert, or a non-numeric value, never fires.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    parsed = parse_alert(alert)
    if parsed is None:
        return False
    operator, threshold = parsed
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "==":
        return value == threshold
    if operator == "!=":
        return value != threshold
    return False


def effective_value(measurement: dict, monitor: dict) -> float | None:
    """The quantity an alert compares: value/of where known, else value."""
    value = measurement.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    denominator = measurement.get("of")
    if denominator is None:
        denominator = monitor.get("of")
    if isinstance(denominator, (int, float)) and not isinstance(
            denominator, bool) and denominator != 0:
        return float(value) / float(denominator)
    return float(value)


def find_monitor(monitors: list, name: str) -> list[dict]:
    """Declared monitors matching ``name`` by measure or question."""
    key = (name or "").strip()
    if not key:
        return []
    hits = []
    for monitor in monitors:
        if not isinstance(monitor, dict):
            continue
        if key == str(monitor.get("measure") or "").strip():
            hits.append(monitor)
        elif key == str(monitor.get("question") or "").strip():
            hits.append(monitor)
    return hits


def matching_measurements(measurements: list[dict], monitor: dict) -> list[dict]:
    """Ledger lines for one monitor, in ledger order."""
    measure = str(monitor.get("measure") or "")
    question = str(monitor.get("question") or "")
    out = []
    for entry in measurements:
        if not isinstance(entry, dict):
            continue
        tag = entry.get("monitor")
        if tag == measure or (question and tag == question):
            out.append(entry)
    return out


def format_number(value: object) -> str:
    """A measurement number as the owner reads it: 3, not 3.0."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def trend_of(previous: float | None, latest: float | None) -> str:
    """The trend arrow between two values, or ``""`` when there is one."""
    if previous is None or latest is None:
        return ""
    if latest > previous:
        return "\u2191"
    if latest < previous:
        return "\u2193"
    return "\u2192"
