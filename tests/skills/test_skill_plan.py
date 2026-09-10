"""The planner's skill: frontmatter and the field names the examples type.

Dry-run acceptance and the v5 refusal lines live in
``tests/front-v5/test_planner_skill.py``. This file still pins that the
skill teaches every key the shipped briefs actually use, so a field can
only be renamed in both places at once.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "foreman-plan" / "SKILL.md"
EXAMPLES = ROOT / "skills" / "foreman-plan" / "examples"


def test_skill_has_frontmatter_name_and_description():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---\n", 2)[1]
    assert "name: foreman-plan" in frontmatter
    assert "description:" in frontmatter


def brief_keys(path: Path) -> set[str]:
    """Every key and table name an example brief actually uses."""
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            keys.add(line)
        elif "=" in line and not line.startswith("#"):
            keys.add(line.split("=", 1)[0].strip())
    return keys


def test_skill_names_every_field_the_examples_use():
    """The skill teaches the validator's field names, not synonyms."""
    text = SKILL.read_text(encoding="utf-8")
    used: set[str] = set()
    for example in sorted(EXAMPLES.iterdir()):
        used |= brief_keys(example / "brief.toml")
    assert used, "no example briefs to derive field names from"
    missing = sorted(key for key in used if key not in text)
    assert not missing, f"skill never names {missing}"
