"""What the installed package must carry.

The launcher reads role prompt templates, pool manifests and pool skills
out of the package directory at run time. A wheel that leaves any of them
behind still imports, still passes every test that runs from the source
checkout, and then refuses every launch on the machine that installed it:
"roles with a template: (none)". So the check is not "does this file
exist" — it does, in the checkout — but "does a package-data glob in
pyproject.toml claim it".
"""
from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "foreman"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
MANIFEST = Path(__file__).resolve().parents[2] / "MANIFEST.in"


def _globs() -> list[str]:
    with open(PYPROJECT, "rb") as handle:
        raw = tomllib.load(handle)
    data = raw["tool"]["setuptools"]["package-data"]
    return list(data["foreman"])


def _claimed(relative: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in globs)


def _non_python_data() -> list[str]:
    """Every non-Python file under the package, package-relative."""
    out = []
    for path in sorted(PACKAGE.rglob("*")):
        if not path.is_file() or path.suffix == ".py":
            continue
        if "__pycache__" in path.parts:
            continue
        out.append(path.relative_to(PACKAGE).as_posix())
    return out


def test_every_data_file_under_the_package_is_claimed():
    globs = _globs()
    unclaimed = [name for name in _non_python_data()
                 if not _claimed(name, globs)]
    assert unclaimed == [], (
        "these files live in the package but no package-data glob claims "
        f"them, so an installed Foreman will not have them: {unclaimed}")


@pytest.mark.parametrize("role", ["muse", "opus", "astra", "grok",
                                  "supervisor", "merge-desk", "foreman"])
def test_every_role_the_launcher_renders_has_a_packaged_template(role):
    from foreman import launch

    template = launch.TEMPLATE_DIR / f"{role}.md"
    assert template.exists(), f"no template for role {role!r}"
    assert _claimed(f"templates/{role}.md", _globs()), (
        f"templates/{role}.md is not claimed by package-data; an installed "
        f"Foreman would refuse to launch {role!r}")


def test_the_source_distribution_carries_the_same_directories():
    """A wheel is built from package-data, an sdist from MANIFEST.in.

    Both doors, or the fix holds for one install path and not the other.
    """
    text = MANIFEST.read_text(encoding="utf-8")
    included = {line.split()[1] for line in text.splitlines()
                if line.startswith("recursive-include")}
    directories = {f"src/foreman/{name}" for name in
                   {Path(rel).parts[0] for rel in _non_python_data()
                    if len(Path(rel).parts) > 1}}
    missing = sorted(directories - included)
    assert missing == [], (
        f"MANIFEST.in does not include {missing}; the sdist would ship "
        "without them")
