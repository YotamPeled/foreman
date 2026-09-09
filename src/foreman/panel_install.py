"""Install and uninstall the Foreman panel plugin.

In the style of the agent-workspaces installer: `install`/`uninstall` are
short executables at the repository root delegating here, every step is
safe to run twice, and uninstall reverses exactly what install did.

Three steps, all under the owner's home (never a packaged file):

  symlink       ~/.config/omarchy/plugins/foreman/ -> <repo>/plugin/foreman/
  registration  one {"id": "foreman"} entry in shell.json's bar layout, which
                is where the shell looks: PluginRegistry scans
                ~/.config/omarchy/plugins/*/manifest.json, and a plugin id
                referenced from bar.layout.* counts as enabled, for both its
                bar widget and (via computePanelEntries in shell.qml) its
                panel. The shell's own enable path adds only the bar entry
                for a bar-widget kind, so install does the same: nothing in
                plugins[].
  binding       the Super+M block through panel_binding.py.

Exact reversal: the bindings file is byte-exact through panel_binding's own
append-only edit. shell.json has no comment syntax to mark with, so install
records the file's exact bytes (plus what it wrote and which containers it
invented) in install.json beside them; when nothing else touched the file
since, uninstall puts those bytes back verbatim, and a file install created
is deleted. When the owner edited shell.json in between, uninstall instead
lifts only our entry out and keeps their edits, saying so.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from foreman import panel_binding
except ImportError:  # run as a script: `python3 src/foreman/panel_install.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from foreman import panel_binding

PLUGIN_ID = "foreman"

#: Bar sections and the anchor the shell itself inserts after, from
#: PluginRegistry.barTarget: a widget with no placement goes after these.
ANCHORS = {"left": "omarchy.workspaces", "center": "omarchy.weather",
           "right": "omarchy.tray"}

NO_RELOAD_ENV = "FOREMAN_NO_RELOAD"


# ---------- paths ----------

def plugin_link(home: str | Path) -> Path:
    return Path(home) / ".config" / "omarchy" / "plugins" / PLUGIN_ID


def shell_json_path(home: str | Path) -> Path:
    return Path(home) / ".config" / "omarchy" / "shell.json"


def bindings_path(home: str | Path) -> Path:
    return Path(home) / ".config" / "hypr" / "bindings.lua"


def record_path(home: str | Path) -> Path:
    return Path(home) / ".local" / "state" / "foreman" / "install.json"


def repo_plugin_dir(repo: str | Path) -> Path:
    return Path(repo) / "plugin" / PLUGIN_ID


def default_section(repo: str | Path) -> str:
    """The bar section our manifest asks for, as the shell would read it."""
    try:
        manifest = json.loads((Path(repo) / "plugin" / PLUGIN_ID
                               / "manifest.json").read_text())
        section = manifest.get("barWidget", {}).get("defaultSection", "")
    except (OSError, ValueError, AttributeError):
        return "center"
    return section if section in ("left", "center", "right") else "center"


# ---------- filesystem ----------

def _tidy_parents(home: str | Path, start: Path) -> None:
    """Remove dirs install created, innermost out while they are empty.

    Only empty directories go, and never above home, so on a real machine
    this is a no-op wherever the owner keeps anything else.
    """
    home = Path(home).resolve()
    directory = start.resolve() if start.exists() or start.is_symlink() \
        else start.parent.resolve()
    if not directory.is_dir():
        directory = directory.parent
    while directory != home and home in directory.parents:
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


# ---------- record ----------

def _read_record(home: str | Path) -> dict:
    try:
        data = json.loads(record_path(home).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_record(home: str | Path, data: dict) -> None:
    if not data:
        try:
            record_path(home).unlink()
        except OSError:
            return
        _tidy_parents(home, record_path(home))
        return
    path = record_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _forget(home: str | Path, key: str) -> None:
    data = _read_record(home)
    if key in data:
        del data[key]
        _write_record(home, data)


# ---------- shell.json ----------

class BadJson(Exception):
    pass


def _load_shell_json(path: Path) -> dict | None:
    """The parsed shell.json, None when the file does not exist."""
    try:
        text = path.read_text()
    except OSError:
        return None
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise BadJson(str(path))
    if not isinstance(data, dict):
        raise BadJson(str(path))
    return data


def _layout_sections(doc: dict) -> list:
    layout = doc.get("bar", {}).get("layout", {}) \
        if isinstance(doc.get("bar"), dict) else {}
    if not isinstance(layout, dict):
        return []
    return [(name, items) for name, items in layout.items()
            if isinstance(items, list)]


def _widget_id(entry) -> str:
    return entry.get("id") if isinstance(entry, dict) else entry


def _has_bar_entry(doc: dict) -> bool:
    return any(_widget_id(entry) == PLUGIN_ID
               for _, items in _layout_sections(doc) for entry in items)


def _has_plugin_entry(doc: dict) -> bool:
    plugins = doc.get("plugins", [])
    return isinstance(plugins, list) and \
        any(_widget_id(entry) == PLUGIN_ID for entry in plugins)


def _dump(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"


def install_shell_json(home: str | Path, repo: str | Path) -> str:
    """Add our bar-layout entry, recording the exact bytes for uninstall."""
    path = shell_json_path(home)
    try:
        doc = _load_shell_json(path)
    except BadJson:
        return "skipped: %s is not valid JSON, fix it and run again" % path
    existed = path.exists()
    if doc is None:
        doc, existed = {}, False
    if _has_bar_entry(doc):
        return "already placed"
    section = default_section(repo)
    bar = doc.get("bar")
    if bar is None:
        doc["bar"], made_bar = {}, True
    elif not isinstance(bar, dict):
        return "skipped: bar in %s is not an object" % path
    else:
        made_bar = False
    layout = doc["bar"].get("layout")
    if layout is None:
        doc["bar"]["layout"], made_layout = {}, True
    elif not isinstance(layout, dict):
        return "skipped: bar.layout in %s is not an object" % path
    else:
        made_layout = False
    items = doc["bar"]["layout"].get(section)
    if items is None:
        items = doc["bar"]["layout"][section] = []
        made_section = True
    elif not isinstance(items, list):
        return "skipped: bar.layout.%s in %s is not a list" % (section, path)
    else:
        made_section = False
    made = ([k for k, flag in (("bar", made_bar), ("layout", made_layout),
                               (section, made_section)) if flag])
    entry = {"id": PLUGIN_ID}
    anchor = ANCHORS[section]
    try:
        at = next(i for i, w in enumerate(items)
                  if _widget_id(w) == anchor) + 1
    except StopIteration:
        at = len(items)
    items.insert(at, entry)
    new_text = _dump(doc)
    if existed:
        try:
            original = path.read_bytes()
        except OSError:
            return "skipped: cannot read %s" % path
    else:
        original = None
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text)
    data = _read_record(home)
    data["shell_json"] = {
        "created": not existed,
        "original_b64": (base64.b64encode(original).decode("ascii")
                         if original is not None else None),
        "installed_sha256": hashlib.sha256(
            new_text.encode("utf-8")).hexdigest(),
        "made": made,
        "section": section,
    }
    _write_record(home, data)
    return "added to the %s section" % section


def _prune(doc: dict, made: list) -> None:
    """Drop containers install invented, if nothing moved into them."""
    layout = doc.get("bar", {}).get("layout", {}) \
        if isinstance(doc.get("bar"), dict) else None
    section = next((k for k in made if k not in ("bar", "layout")), None)
    if section is not None and isinstance(layout, dict) \
            and layout.get(section) == []:
        layout.pop(section)
    if "layout" in made and doc.get("bar", {}).get("layout") == {}:
        doc["bar"].pop("layout")
    if "bar" in made and doc.get("bar") == {}:
        doc.pop("bar")


def _surgical_remove(doc: dict, made: list) -> bool:
    """Lift our entries out, keeping everything else. True when found."""
    found = False
    for _, items in _layout_sections(doc):
        kept = [w for w in items if _widget_id(w) != PLUGIN_ID]
        if len(kept) != len(items):
            items[:] = kept
            found = True
    plugins = doc.get("plugins")
    if isinstance(plugins, list):
        kept = [w for w in plugins if _widget_id(w) != PLUGIN_ID]
        if len(kept) != len(plugins):
            doc["plugins"] = kept
            found = True
    if found:
        _prune(doc, made)
    return found


def remove_shell_json(home: str | Path) -> str:
    """Reverse install_shell_json: exact bytes when untouched, surgical
    removal keeping the owner's later edits otherwise."""
    path = shell_json_path(home)
    saved = _read_record(home).get("shell_json")
    if saved is None:
        if not path.exists():
            return "was not placed"
        try:
            doc = _load_shell_json(path)
        except BadJson:
            return "skipped: %s is not valid JSON, fix it and run again" % path
        if doc is None or not (_has_bar_entry(doc)
                               or _has_plugin_entry(doc)):
            return "was not placed"
        if _surgical_remove(doc, []):
            path.write_text(_dump(doc))
            return "removed"
        return "was not placed"
    if not path.exists():
        _forget(home, "shell_json")
        return "none found"
    try:
        current = path.read_bytes()
    except OSError:
        return "skipped: cannot read %s" % path
    if hashlib.sha256(current).hexdigest() == saved.get("installed_sha256"):
        # Untouched since install: the recorded bytes go back verbatim.
        if saved.get("created"):
            try:
                path.unlink()
            except OSError:
                pass
            else:
                _tidy_parents(home, path)
        else:
            path.write_bytes(base64.b64decode(saved["original_b64"]))
        _forget(home, "shell_json")
        return "removed"
    try:
        doc = json.loads(current.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "skipped: %s is not valid JSON, fix it and run again" % path
    if not isinstance(doc, dict) or not (_has_bar_entry(doc)
                                         or _has_plugin_entry(doc)):
        # The owner already took our entry out: leave their file alone.
        _forget(home, "shell_json")
        return "was not placed"
    _surgical_remove(doc, saved.get("made", []))
    path.write_text(_dump(doc))
    _forget(home, "shell_json")
    return "removed, kept edits made after the install"


# ---------- symlink ----------

def install_link(home: str | Path, repo: str | Path) -> str:
    link, src = plugin_link(home), repo_plugin_dir(repo)
    if link.is_symlink() and link.resolve() == src.resolve():
        return "already there"
    if os.path.lexists(link):
        return ("skipped: %s exists and is not ours; "
                "move it aside and run again" % link)
    if not src.is_dir():
        return "skipped: %s not found in the checkout" % src
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(src.resolve())
    return "added"


def remove_link(home: str | Path, repo: str | Path) -> str:
    link, src = plugin_link(home), repo_plugin_dir(repo)
    if not os.path.lexists(link):
        return "none found"
    if link.is_symlink():
        try:
            same = link.resolve() == src.resolve()
        except OSError:
            same = False
        missing = False
        try:
            link.resolve(strict=True)
        except OSError:
            missing = True
        if same or missing:
            link.unlink()
            _tidy_parents(home, link)
            return "removed"
    return ("skipped: %s is not ours; remove it by hand and run again"
            % link)


# ---------- binding ----------

def install_binding_step(home: str | Path) -> str:
    return panel_binding.install_binding(bindings_path(home))


def remove_binding_step(home: str | Path) -> str:
    outcome = panel_binding.uninstall_binding(bindings_path(home))
    if outcome == "removed" and not bindings_path(home).exists():
        _tidy_parents(home, bindings_path(home))
    return outcome


# ---------- reload ----------

def _reload() -> None:
    if os.environ.get(NO_RELOAD_ENV):
        return
    if shutil.which("hyprctl") is None:
        return
    try:
        subprocess.run(["hyprctl", "reload"], capture_output=True,
                       timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    # The shell hot-reloads shell.json and anything under plugins/ on save;
    # no shell restart needed.


# ---------- commands ----------

def _say(*parts: str) -> None:
    print(" ", *parts, flush=True)


def _step(label: str, func, *args) -> str:
    try:
        outcome = func(*args)
    except BadJson as bad:
        outcome = "skipped: %s is not valid JSON, fix it and run again" \
            % bad.args[0]
    except OSError as error:
        outcome = "skipped: %s" % error
    _say(label, "…", outcome)
    return outcome


def install(repo: str | Path, home: str | Path | None = None) -> int:
    home = Path(home) if home is not None else Path.home()
    print("Foreman panel")
    _step("plugin link", install_link, home, repo)
    _step("bar registration", install_shell_json, home, repo)
    _step("keybinding", install_binding_step, home)
    print()
    _say("Super+M summons the panel.")
    _reload()
    print("\nDone.")
    return 0


def uninstall(repo: str | Path, home: str | Path | None = None) -> int:
    home = Path(home) if home is not None else Path.home()
    print("Removing the Foreman panel")
    _step("bar registration", remove_shell_json, home)
    _step("keybinding", remove_binding_step, home)
    _step("plugin link", remove_link, home, repo)
    _reload()
    print("\nDone.")
    return 0


USAGE = "usage: panel_install.py install|uninstall <repo> [--home DIR]"


def main(argv: list[str]) -> int:
    verb, repo, home = None, None, None
    rest = list(argv)
    if "--home" in rest:
        i = rest.index("--home")
        try:
            home = rest[i + 1]
        except IndexError:
            print(USAGE, file=sys.stderr)
            return 2
        del rest[i:i + 2]
    if len(rest) == 2:
        verb, repo = rest
    else:
        print(USAGE, file=sys.stderr)
        return 2
    if verb == "install":
        return install(repo, home)
    if verb == "uninstall":
        return uninstall(repo, home)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
