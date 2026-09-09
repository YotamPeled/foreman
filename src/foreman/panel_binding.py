"""Super+M binding for the Foreman panel.

Installs one marked block into the owner's Hyprland bindings file, in the
marked-edit style the agent-workspaces installer uses: a block uninstall
removes exactly, reinstall is a no-op, and damaged markers are left for a
person rather than rewritten.

The binding never steals a key: when Super+M is already bound elsewhere in
the file, install refuses and leaves the file alone. The next task wires
these two functions into the panel's install script.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

BEGIN = "-- >>> foreman-panel"
END = "-- <<< foreman-panel"
NOTE = "-- Managed by the Foreman panel. Edits inside this block are lost on reinstall."

BINDING_LINE = 'o.bind("SUPER + M", "Foreman panel", "omarchy-shell shell toggle foreman")'

BINDINGS_BLOCK = f"""{BEGIN}
{NOTE}
-- Summon and dismiss the Foreman panel.
{BINDING_LINE}
{END}"""

#: Super+M on a live (non-comment) bindings line. Comment lines start with
#: `--` after optional whitespace and never count: the stock file's
#: commented examples mention other Super combinations.
_SUPER_M_RE = re.compile(r"\bSUPER\s*\+\s*M\b", re.IGNORECASE)


def bindings_path() -> Path:
    return Path.home() / ".config" / "hypr" / "bindings.lua"


def _without_own_block(text: str) -> str:
    return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), "",
                  text, flags=re.S)


def super_m_taken(text: str) -> bool:
    """True when a live line outside our own block binds Super+M."""
    for line in _without_own_block(text).splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        if _SUPER_M_RE.search(line):
            return True
    return False


def _markers_ok(text: str, path: str) -> bool:
    if not text.count(BEGIN) and not text.count(END):
        return True
    if (text.count(BEGIN) == 1 and text.count(END) == 1
            and text.index(BEGIN) < text.index(END)):
        return True
    print(f"    the {BEGIN} / {END} markers in {path} are "
          f"{text.count(BEGIN)} and {text.count(END)}; "
          "remove the block by hand and run again")
    return False


def _refused(target: Path) -> str:
    return (f"refused: Super+M is already bound in {target}; "
            "leaving the file alone")


def install_binding(path: str | Path | None = None) -> str:
    """Add the Super+M block, refusing when Super+M is already bound.

    The refusal holds on every install, including a reinstall over an
    existing block: our own block never counts, so a competing binding
    anywhere outside it still refuses.
    """
    target = Path(path).expanduser() if path else bindings_path()
    current = target.read_text() if target.exists() else ""
    if not _markers_ok(current, str(target)):
        return "skipped, markers are damaged"
    if super_m_taken(current):
        return _refused(target)
    if BEGIN in current:
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END),
                     BINDINGS_BLOCK, current, flags=re.S)
        if new == current:
            return "already there"
    else:
        new = current.rstrip("\n") + "\n\n" + BINDINGS_BLOCK + "\n"
        if new == current:
            return "already there"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new)
    return "added" if BEGIN not in current else "reinstalled"


def uninstall_binding(path: str | Path | None = None) -> str:
    """Remove exactly the block install added, nothing else.

    A block whose content no longer matches what install wrote — a line
    the owner added inside the markers, for example — is left for a
    person rather than destroyed.
    """
    target = Path(path).expanduser() if path else bindings_path()
    if not target.exists():
        return "none found"
    current = target.read_text()
    if not _markers_ok(current, str(target)):
        return "skipped, markers are damaged"
    found = re.search(re.escape(BEGIN) + r".*?" + re.escape(END),
                      current, flags=re.S)
    if not found:
        return "none found"
    if found.group(0) != BINDINGS_BLOCK:
        return ("skipped, the managed block in "
                f"{target} was edited by hand; "
                "remove it by hand and run again")
    new = re.sub(r"\n*" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n*",
                 "\n", current, flags=re.S)
    if new == current:
        return "none found"
    target.write_text(new)
    return "removed"


def binding_state(path: str | Path | None = None) -> str:
    """One-word report for the install script: present, taken or free."""
    target = Path(path).expanduser() if path else bindings_path()
    if not target.exists():
        return "free"
    current = target.read_text()
    if BEGIN in current:
        return "present"
    return "taken" if super_m_taken(current) else "free"


if __name__ == "__main__":
    import sys

    verb = sys.argv[1] if len(sys.argv) > 1 else "state"
    if verb == "install":
        print(install_binding())
    elif verb == "uninstall":
        print(uninstall_binding())
    else:
        print(binding_state())
