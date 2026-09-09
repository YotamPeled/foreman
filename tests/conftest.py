"""No test reaches this machine's systemd. Enforced here, not per file.

rul-tx35izr says no test or isolated-world run touches the machine's
systemd units. Per-file discipline does not hold that line: when the
collector's tick learned to spawn a turn carrier, every test in every
other file that drove a tick began starting real transient units on the
developer's machine, and every one of those tests passed. Eleven units
were found running on this machine, named after test fixture sessions,
before this file existed.

So the doors that reach systemd are shut here for the whole suite, and a
test that means to exercise one opens it deliberately by substituting
its own double. Reaching a shut door is a failure with the argv in the
message, never a silent unit.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SMOKE_UNITS_PATH = Path(__file__).resolve().parent / "smoke_units.py"
_SMOKE_UNITS_NAME = "foreman_test_smoke_units"
_spec = importlib.util.spec_from_file_location(
    _SMOKE_UNITS_NAME, _SMOKE_UNITS_PATH)
_smoke_units_mod = importlib.util.module_from_spec(_spec)
sys.modules[_SMOKE_UNITS_NAME] = _smoke_units_mod
assert _spec.loader is not None
_spec.loader.exec_module(_smoke_units_mod)
SmokeUnitReaper = _smoke_units_mod.SmokeUnitReaper
list_foreman_units = _smoke_units_mod.list_foreman_units
leftover_smoke_units = _smoke_units_mod.leftover_smoke_units

#: module attribute -> what production calls it for. Each is the last
#: point before a real `systemd-run` or `systemctl`.
SYSTEMD_DOORS = (
    ("foreman.collector", "_default_turn_spawn"),
    ("foreman.headless", "_default_spawn"),
    ("foreman.headless", "free_unit_name"),
)


#: The real callables, kept so a test that means to exercise a door's own
#: body can ask for it by name through the ``real_door`` fixture.
_REAL: dict[str, object] = {}


@pytest.fixture()
def real_door():
    """The unpatched body of a shut door, by "module.attribute".

    For the tests that exist to check what a door builds and hands on —
    they still must not let it reach systemd, so they double what the
    door calls (``subprocess.run``) rather than the door itself.
    """
    return lambda name: _REAL[name]


@pytest.fixture(autouse=True)
def _no_live_box_count(monkeypatch):
    """Capacity must not read this machine's process table.

    The box count walks vendor processes whoever started them. A test
    that does not pass a fake table would otherwise see this machine's
    grok (and refuse a launch onto a cap of one). The collector still
    reads ``procs.snapshot`` for the children it started; only
    capacity's default table is substituted.
    """
    import foreman.capacity as capacity

    monkeypatch.setattr(capacity, "_snapshot", lambda: {})


@pytest.fixture(autouse=True)
def _no_systemd(monkeypatch, request):
    """Shut every door to systemd unless the test opened one itself."""
    import importlib

    for module_name, attribute in SYSTEMD_DOORS:
        module = importlib.import_module(module_name)
        if not hasattr(module, attribute):
            continue
        _REAL[f"{module_name}.{attribute}"] = getattr(module, attribute)

        def refuse(*args, _door=f"{module_name}.{attribute}", **kwargs):
            raise AssertionError(
                f"{_door} would start a real systemd unit: "
                f"{args!r} {kwargs!r}. Substitute it in the test "
                f"(rul-tx35izr: no test touches this machine's units).")

        monkeypatch.setattr(module, attribute, refuse)


# Smoke tests are the exception that reach systemd. They record each
# session they launched; teardown stops the unit and clears its failed
# state, pass or fail. The ordinary suite never uses this.


@pytest.fixture
def smoke_units():
    """Stop and reset-failed every unit this smoke test started.

    Runs on failure too. The process-group kill in the smoke test stays:
    that is a stuck vendor; this is the unit name.
    """
    reaper = SmokeUnitReaper()
    try:
        yield reaper
    finally:
        reaper.teardown()


@pytest.fixture(scope="session")
def _smoke_unit_census():
    """The user manager's ``foreman-*`` set must not grow across a smoke run."""
    before = list_foreman_units()
    yield
    after = list_foreman_units()
    grown = leftover_smoke_units(before, after)
    assert not grown, (
        "smoke tests left new foreman units: " + ", ".join(sorted(grown))
    )


@pytest.fixture(autouse=True)
def _smoke_unit_census_bind(request):
    if request.node.get_closest_marker("smoke") is None:
        return
    request.getfixturevalue("_smoke_unit_census")
