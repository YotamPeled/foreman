"""`foreman measure`: a supervisor files a monitor measurement.

A measurement with no command behind it is refused for the same reason a
CONFIRMED claim with no command is: running the command is what makes the
number a measurement instead of a guess. A monitor the front's brief does
not declare is refused by name. Only the front's own supervisor (or the
owner) may call it.
"""

from __future__ import annotations

import argparse

from . import caller, cli, entities, fronts, monitors, paths, store
from .caller import Refusal


def _parse_number(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def measure_main(front: str, monitor: str, value: str | None,
                 of: str | None = None,
                 command: str | None = None,
                 output: str | None = None) -> int:
    verb = "measure"
    me, violations = caller.resolve(verb)
    front_name = (front or "").strip()
    monitor_name = (monitor or "").strip()
    if not front_name:
        violations.append("field 'front' is required")
    if not monitor_name:
        violations.append("field 'monitor' is required")
    record = fronts.read_front_record(front_name) if front_name else None
    if front_name and record is None:
        violations.append(f"unknown front '{front_name}'")
    caller.check_front_supervisor(me, front_name or None, verb,
                                  violations=violations)
    number = _parse_number(value) if value is not None else None
    if value is None or (isinstance(value, str) and not value.strip()):
        violations.append("field '--value' is required")
    elif number is None:
        violations.append(f"field '--value' must be a number "
                          f"(got '{value}')")
    denominator: float | None = None
    if of is not None:
        denominator = _parse_number(of)
        if denominator is None:
            violations.append(f"field '--of' must be a number "
                              f"(got '{of}')")
    if not (command or "").strip():
        violations.append("field '--command' is required "
                          "(a measurement with no command behind it "
                          "is not a measurement)")
    if not (output or "").strip():
        violations.append("field '--output' is required "
                          "(a measurement with no output behind it "
                          "is not a measurement)")
    declared: dict | None = None
    if record is not None and monitor_name:
        candidates = record.get("monitors")
        candidates = candidates if isinstance(candidates, list) else []
        hits = monitors.find_monitor(candidates, monitor_name)
        if len(hits) > 1:
            violations.append(f"field 'monitor' is ambiguous "
                              f"({monitor_name!r} names "
                              f"{len(hits)} monitors on front "
                              f"'{front_name}')")
        elif not hits:
            violations.append(f"unknown monitor '{monitor_name}' "
                              f"on front '{front_name}'")
        else:
            declared = hits[0]
    if violations:
        return Refusal(violations).report()
    assert record is not None and declared is not None
    assert number is not None
    who = caller.by_line(me)
    canonical = str(declared.get("measure") or "").strip() or monitor_name
    from . import ids, progress as progress_mod

    head, base = progress_mod._binding_shas(front_name, None)
    store.append_ledger(
        paths.front_measurements_path(front_name),
        entities.Measurement(
            id=ids.mint("measurement"),
            monitor=canonical,
            value=number,
            of=denominator,
            status="",
            command=(command or "").strip(),
            output_ref=(output or "").strip(),
            head=head,
            base=base,
        ).to_dict(),
        session_id=who,
    )
    if denominator is None:
        print(f"measured {canonical} {monitors.format_number(number)} "
              f"on {front_name}")
    else:
        print(f"measured {canonical} {monitors.format_number(number)}/"
              f"{monitors.format_number(denominator)} on {front_name}")
    return 0


def add_measure_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("front", help="front the monitor belongs to")
    sub.add_argument("monitor", help="monitor measure (or question) "
                                    "as declared in the brief")
    sub.add_argument("--value", default=None,
                     help="measured value (required, a number)")
    sub.add_argument("--of", default=None,
                     help="denominator the value is out of (optional)")
    sub.add_argument("--command", default=None,
                     help="measure command that was run (required)")
    sub.add_argument("--output", default=None,
                     help="command output, or a path to it (required)")


@cli.subcommand("measure", help="File a monitor measurement.")
def _measure_entry(args: argparse.Namespace) -> int:
    return measure_main(args.front, args.monitor, args.value,
                        of=args.of, command=args.command,
                        output=args.output)


_measure_entry.add_arguments = add_measure_arguments  # type: ignore[attr-defined]
