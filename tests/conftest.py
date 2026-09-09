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

import pytest

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
