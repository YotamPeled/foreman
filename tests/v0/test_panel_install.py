"""Panel install: the binding round-trips byte-exact and the three install
steps reverse exactly, all against temporary homes — never the real one.

Each test names the break it catches: the Astra-found defect (a created
file left behind, trailing newlines collapsed), a stolen Super+M, a
hand-edited block, damaged markers, and every install step's reversal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foreman import panel_binding as binding
from foreman import panel_install as install_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FOREMAN_NO_RELOAD", "1")
    return tmp_path


def bindings(home: Path) -> Path:
    return install_mod.bindings_path(home)


def shell_json(home: Path) -> Path:
    return install_mod.shell_json_path(home)


def write_bindings(home: Path, data: bytes) -> Path:
    target = bindings(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


# ---------- panel_binding: exact reversal ----------

def test_missing_bindings_file_returns_to_missing(tmp_path):
    """Install then uninstall on a file that did not exist leaves no file:
    the defect the review caught."""
    target = tmp_path / "bindings.lua"
    assert binding.install_binding(target) == "added"
    assert binding.install_binding(target) == "already there"
    assert binding.uninstall_binding(target) == "removed"
    assert not target.exists()


def test_empty_bindings_file_returns_to_empty(tmp_path):
    """A file that existed but was empty comes back empty, not deleted."""
    target = tmp_path / "bindings.lua"
    target.write_bytes(b"")
    assert binding.install_binding(target) == "added"
    assert binding.uninstall_binding(target) == "removed"
    assert target.read_bytes() == b""


@pytest.mark.parametrize("seed", [
    b"x = 1\n",
    b"x = 1",
    b"x = 1\n\n",
    b"x = 1\n\n\n",
    b"-- a comment mentioning SUPER + M\n",
])
def test_bindings_roundtrip_keeps_exact_bytes(tmp_path, seed):
    """Trailing newlines (or their absence) survive install + uninstall."""
    target = tmp_path / "bindings.lua"
    target.write_bytes(seed)
    assert binding.install_binding(target) == "added"
    assert binding.install_binding(target) == "already there"
    assert binding.uninstall_binding(target) == "removed"
    assert target.read_bytes() == seed


def test_install_refuses_taken_super_m_and_leaves_file(tmp_path):
    """A competing Super+M binding refuses the install with the file
    untouched — on a first install and on a reinstall over our block."""
    target = tmp_path / "bindings.lua"
    taken = b'o.bind("SUPER + M", "mine", "x")\n'
    target.write_bytes(taken)
    assert "refused" in binding.install_binding(target)
    assert target.read_bytes() == taken
    target.write_bytes(b"")
    assert binding.install_binding(target) == "added"
    with target.open("ab") as handle:
        handle.write(b'o.bind("super+m", "mine", "x")\n')
    poisoned = target.read_bytes()
    assert "refused" in binding.install_binding(target)
    assert target.read_bytes() == poisoned


def test_uninstall_refuses_hand_edited_block(tmp_path):
    """A block someone edited by hand stays for a person, not the tool."""
    target = tmp_path / "bindings.lua"
    assert binding.install_binding(target) == "added"
    edited = target.read_text().replace("Summon and dismiss",
                                        "Summon, dismiss, adore")
    target.write_text(edited)
    assert "by hand" in binding.uninstall_binding(target)
    assert target.read_text() == edited


def test_damaged_markers_are_reported_not_rewritten(tmp_path):
    """Doubled or reversed markers refuse both directions, files intact."""
    target = tmp_path / "bindings.lua"
    damaged = (binding.BEGIN + "\n" + binding.BEGIN + "\n").encode()
    target.write_bytes(damaged)
    assert "damaged" in binding.install_binding(target)
    assert "damaged" in binding.uninstall_binding(target)
    assert target.read_bytes() == damaged


# ---------- panel_install: the three steps ----------

STOCK_SHELL = (
    b'{\n    "version": 1,\n    "bar": {\n        "layout": {\n            "right": [\n                {\n                    "id": "omarchy.tray"\n                }\n            ]\n        }\n    },\n    "plugins": []\n}'
)


def seed(home: Path, shell: bytes | None = STOCK_SHELL,
         binds: bytes | None = b"-- stock\n") -> None:
    if shell is not None:
        path = shell_json(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(shell)
    if binds is not None:
        write_bindings(home, binds)


def snapshot(home: Path) -> dict:
    """Every file and link under home, by exact bytes or link target."""
    out = {}
    for path in sorted(home.rglob("*")):
        rel = str(path.relative_to(home))
        if path.is_symlink():
            out[rel] = ("link", str(path.resolve()))
        elif path.is_file():
            out[rel] = ("file", path.read_bytes())
    return out


def test_full_roundtrip_is_byte_exact(home):
    """Install, install again (a no-op), uninstall: the tree is what it
    was, byte for byte, with nothing left behind."""
    seed(home)
    before = snapshot(home)
    repo = Path(__file__).resolve().parents[2]
    assert install_mod.install(repo, home) == 0
    link = home / ".config" / "omarchy" / "plugins" / "foreman"
    assert link.is_symlink()
    doc = json.loads(shell_json(home).read_text())
    right = doc["bar"]["layout"]["right"]
    assert {"id": "foreman"} in right
    assert right.index({"id": "foreman"}) == (
        right.index({"id": "omarchy.tray"}) + 1)
    assert binding.BEGIN in bindings(home).read_text()
    assert install_mod.install(repo, home) == 0
    mid = snapshot(home)
    assert mid != before
    assert mid[".config/hypr/bindings.lua"][0] == "file"
    assert install_mod.uninstall(repo, home) == 0
    assert snapshot(home) == before


def test_roundtrip_from_nothing(home):
    """Neither file exists: install creates, uninstall deletes, the
    record included."""
    repo = Path(__file__).resolve().parents[2]
    assert install_mod.install(repo, home) == 0
    assert shell_json(home).is_file()
    assert bindings(home).is_file()
    assert install_mod.uninstall(repo, home) == 0
    assert snapshot(home) == {}


def test_second_uninstall_is_a_noop(home):
    seed(home)
    repo = Path(__file__).resolve().parents[2]
    install_mod.install(repo, home)
    assert install_mod.uninstall(repo, home) == 0
    after = snapshot(home)
    assert install_mod.uninstall(repo, home) == 0
    assert snapshot(home) == after


def test_taken_key_leaves_bindings_file_untouched(home):
    seed(home, binds=b'o.bind("SUPER + M", "mine", "x")\n')
    repo = Path(__file__).resolve().parents[2]
    assert install_mod.install(repo, home) == 0
    assert bindings(home).read_bytes() == b'o.bind("SUPER + M", "mine", "x")\n'
    before = dict(snapshot(home))
    before_bindings = before[".config/hypr/bindings.lua"]
    assert install_mod.uninstall(repo, home) == 0
    after = snapshot(home)
    assert after[".config/hypr/bindings.lua"] == before_bindings


def test_foreign_link_is_never_destroyed(home):
    """Something else already lives at the plugin path: install skips the
    link step and uninstall leaves it alone."""
    repo = Path(__file__).resolve().parents[2]
    link = home / ".config" / "omarchy" / "plugins" / "foreman"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.mkdir()
    (link / "manifest.json").write_text("{}")
    assert "skipped" in install_mod.install_link(home, repo)
    assert "skipped" in install_mod.remove_link(home, repo)
    assert (link / "manifest.json").read_text() == "{}"


def test_owner_edits_between_survive_uninstall(home):
    """shell.json edited after install: uninstall lifts only our entry and
    says the later edits were kept."""
    seed(home)
    repo = Path(__file__).resolve().parents[2]
    install_mod.install(repo, home)
    doc = json.loads(shell_json(home).read_text())
    doc["bar"]["layout"]["right"].append({"id": "omarchy.clock"})
    shell_json(home).write_text(json.dumps(doc, indent=2) + "\n")
    outcome = install_mod.remove_shell_json(home)
    assert "kept edits" in outcome
    doc = json.loads(shell_json(home).read_text())
    ids = [w["id"] for w in doc["bar"]["layout"]["right"]]
    assert "foreman" not in ids
    assert "omarchy.clock" in ids
