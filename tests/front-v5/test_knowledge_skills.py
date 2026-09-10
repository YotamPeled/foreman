"""Skills derive from docs/knowledge; a test fails when they disagree.

The break: change one model line in skills/foreman-pool-grok/SKILL.md
without changing models.md. The derive check must go red naming
foreman-pool-grok and the heading.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from foreman.skills_derive import (
    amendment_offenders,
    disagreements,
    derive,
    knowledge_amendment_offenders,
    main,
)

ROOT = Path(__file__).resolve().parents[2]


def _layout(tmp_path: Path) -> Path:
    """A copy of the knowledge files and skills, so a mutation stays local."""
    shutil.copytree(ROOT / "docs" / "knowledge",
                    tmp_path / "docs" / "knowledge")
    shutil.copytree(ROOT / "skills", tmp_path / "skills")
    return tmp_path


def test_derived_sections_agree_with_knowledge():
    found = disagreements(ROOT)
    assert not found, found[0].message()


def test_derive_check_names_skill_and_heading_on_difference(tmp_path):
    root = _layout(tmp_path)
    derive(root)
    assert not disagreements(root), disagreements(root)[0].message()
    skill = root / "skills" / "foreman-pool-grok" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    mutated = text.replace("Best builder", "Best builder mutated", 1)
    assert mutated != text, "grok skill lost the models.md grok body"
    skill.write_text(mutated, encoding="utf-8")
    found = disagreements(root)
    assert found, "derive check stayed green after a grok skill edit"
    msg = found[0].message()
    assert "foreman-pool-grok" in msg, msg
    assert "Grok 4.6 (high) — builder" in msg, msg


def test_derive_check_cli_names_the_first_disagreement(tmp_path, capsys):
    root = _layout(tmp_path)
    derive(root)
    skill = root / "skills" / "foreman-pool-grok" / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace(
            "Best builder", "Best builder mutated", 1),
        encoding="utf-8")
    code = main(["--check"], root=root)
    assert code == 1
    err = capsys.readouterr().err
    assert "foreman-pool-grok" in err, err
    assert "Grok 4.6 (high) — builder" in err, err


def test_knowledge_amendments_carry_date_and_ruling_id():
    bad = knowledge_amendment_offenders(ROOT)
    assert not bad, (
        "amendments missing a date or rul- id:\n" + "\n".join(bad)
    )


def test_amendment_check_names_the_offending_line():
    text = (
        "# Models: observed behaviour\n"
        "\n"
        "Every line here comes from an incident.\n"
        "\n"
        "## Grok 4.6 (high) — builder\n"
        "- Best builder on a named spec.\n"
        "\n"
        "2026-09-10 changed the grok timeout.\n"
    )
    found = amendment_offenders(text)
    assert found, "a dated line with no rul- id must fail"
    _lineno, line = found[0]
    assert "2026-09-10 changed the grok timeout." in line
    clean = text.replace(
        "2026-09-10 changed the grok timeout.",
        "2026-09-10 rul-egz7rm5 changed the grok timeout.",
    )
    assert amendment_offenders(clean) == []
