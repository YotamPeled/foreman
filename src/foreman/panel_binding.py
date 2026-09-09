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

    The edit is exactly reversible: a fresh install appends one newline,
    the block and one newline to the untouched original bytes (nothing is
    ever stripped, so trailing blank lines survive), and reinstalling over
    an existing block swaps that span in place. `uninstall_binding`
    removes exactly that unit.
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
    elif current == "":
        if target.exists():
            # Existed but empty: the leading newline keeps this case apart
            # from a file the install created, so uninstall restores the
            # empty file instead of deleting it.
            new = "\n" + BINDINGS_BLOCK + "\n"
        else:
            new = BINDINGS_BLOCK + "\n"
    else:
        new = current + "\n" + BINDINGS_BLOCK + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new)
    return "added" if BEGIN not in current else "reinstalled"


def uninstall_binding(path: str | Path | None = None) -> str:
    """Remove exactly the block install added, nothing else.

    A block whose content no longer matches what install wrote — a line
    the owner added inside the markers, for example — is left for a
    person rather than destroyed.

    Reversal is byte-exact for anything install wrote: a fresh install
    ends the file with the block and one newline, so uninstall strips
    exactly that unit; a file that did not exist before the install does
    not exist after the uninstall. A block sitting mid-file (only a hand
    move puts one there) is removed best-effort: the block span and one
    following newline, leaving the owner's lines alone.
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
    unit = BINDINGS_BLOCK + "\n"
    if current == unit:
        # Install created this file outright: take it back.
        target.unlink()
        return "removed"
    if current == "\n" + unit:
        # Install found an empty file: give the empty file back.
        target.write_text("")
        return "removed"
    suffix = "\n" + unit
    if current.endswith(suffix):
        target.write_text(current[:-len(suffix)])
        return "removed"
    # Mid-file block: drop the span and at most the newline after it.
    if current[found.end():found.end() + 1] == "\n":
        rest = current[:found.start()] + current[found.end() + 1:]
    else:
        rest = current[:found.start()] + current[found.end():]
    target.write_text(rest)
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
