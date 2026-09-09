"""Supervisor and pool skills: coverage against the pool registry.

The break each test catches: a pool ships (or is added) with no supervisor
facing skill; a pool skill names a stale model or stale roles after the
registry moves on; the supervisor skill drops one of the design's nine
steps or a seed rule. Expected values are read from the registry and the
design in this checkout, never hard-coded: a test that hard-codes the pool
list passes while the fifth pool has no skill.

Pool identity comes from ``foreman.pools.names`` (registered adapters plus
valid user pool directories, the same set ``foreman pool list`` prints) and
``foreman.pools.plugins.describe`` (model, roles, timeout for each name).
"""

from __future__ import annotations

from pathlib import Path

from foreman.pools import names as pool_names
from foreman.pools import plugins

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
SUPERVISE = SKILLS / "foreman-supervise" / "SKILL.md"

# One distinctive phrase per seed rule of docs/DESIGN.md section 9. The
# break: the supervisor skill paraphrases a rule away until a headless
# supervisor never meets it. Phrases are matched case-insensitively.
SEED_RULE_PHRASES = [
    "scope, a verify command and a size",  # 1: dispatch needs scope+verify+size
    "PLAUSIBLE",  # 2: CONFIRMED or PLAUSIBLE claims
    "Findings are records with evidence",  # 3
    "Rules travel",  # 4
    "Checkpoint before anything long",  # 5
    "never raised",  # 6: a question a ruling covers is never raised
    "if the panel cannot show it",  # 7: one page
    "Re-look at the plan after every verified job",  # 8
    "bounded (two)",  # 9: review rounds bounded
    "third time",  # 10: defect class halts dispatch
    "under ~10 tool calls",  # 11: delegate by job size
    "never a reservation",  # 12: allocation is a ceiling
    "one job per process",  # 13: headless workers
    "self-contained",  # 14: owner-facing lines
    "Merge to main only when a front is done",  # 15
]


def pool_skill_path(name: str) -> Path:
    return SKILLS / f"foreman-pool-{name}" / "SKILL.md"


def test_pool_skill_exists_for_every_registered_pool():
    missing = [name for name in pool_names()
               if not pool_skill_path(name).is_file()]
    assert not missing, (
        f"pools with no supervisor-facing skill: {missing} "
        f"(expected {SKILLS / 'foreman-pool-<name>'}/SKILL.md for each)"
    )


def test_no_pool_skill_for_an_unregistered_pool():
    registered = set(pool_names())
    extra = sorted(path.parent.name.removeprefix("foreman-pool-")
                   for path in SKILLS.glob("foreman-pool-*/SKILL.md")
                   if path.parent.name.startswith("foreman-pool-")
                   and path.parent.name.removeprefix("foreman-pool-")
                   not in registered)
    assert not extra, (
        f"skills naming pools nothing registers: {extra} "
        f"(registered pools: {sorted(registered)})"
    )


def test_each_pool_skill_names_its_pool_model_and_roles():
    failures = []
    for name in pool_names():
        manifest, _source = plugins.describe(name)
        assert manifest is not None, f"pool '{name}' has no manifest"
        try:
            text = pool_skill_path(name).read_text(encoding="utf-8")
        except FileNotFoundError:
            failures.append(f"{name}: no skill at {pool_skill_path(name)}")
            continue
        if manifest.model not in text:
            failures.append(f"{name}: model '{manifest.model}' not named")
        for role in manifest.roles:
            if role not in text:
                failures.append(f"{name}: role '{role}' not named")
    assert not failures, (
        "pool skills stale against the registry:\n" + "\n".join(failures)
    )


def test_supervisor_skill_names_all_nine_steps_in_order():
    text = SUPERVISE.read_text(encoding="utf-8")
    positions = []
    for step in range(1, 10):
        marker = f"Step {step}"
        assert marker in text, f"supervisor skill lost {marker} (§3)"
        positions.append(text.index(marker))
    assert positions == sorted(positions), "supervisor steps out of order"


def test_supervisor_skill_names_every_seed_rule():
    text = SUPERVISE.read_text(encoding="utf-8").lower()
    missing = [phrase for phrase in SEED_RULE_PHRASES
               if phrase.lower() not in text]
    assert not missing, (
        f"supervisor skill drops seed rules containing: {missing}"
    )


def test_supervisor_skill_names_both_spec_refusals():
    text = SUPERVISE.read_text(encoding="utf-8").lower()
    assert "120" in text, "supervisor skill lost the one-page refusal"
    assert "verification command" in text, (
        "supervisor skill lost the no-verification-command refusal"
    )


# One distinctive phrase per 2026-09-09 methodology ruling. The break: the
# supervisor skill paraphrases a ruling away until a headless supervisor
# specifies from a brief alone. Phrases match substance, not a paragraph.
BEFORE_A_SPEC_PHRASES = [
    "from a brief alone is a guess",  # investigate before specifying
    "WHAT, INPUTS, OUTPUTS",  # the four slots a stranger could verify
    "one deliverable, one unit",  # jobs are slim
    "commit as you go",  # uncommitted work delivered nothing
    "turn some test red",  # mutate before landing
]


def _h2_section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    lines = text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines)
                 if line.startswith(marker))
    end = next((i for i, line in enumerate(lines[start + 1:], start + 1)
                if line.startswith("## ")), len(lines))
    return "".join(lines[start:end])


def test_supervisor_skill_before_a_spec_names_the_methodology():
    text = SUPERVISE.read_text(encoding="utf-8")
    assert "## Before a spec" in text, (
        "supervisor skill lost ## Before a spec"
    )
    section = _h2_section(text, "Before a spec")
    lowered = " ".join(section.split()).lower()
    missing = [phrase for phrase in BEFORE_A_SPEC_PHRASES
               if phrase.lower() not in lowered]
    assert not missing, (
        f"'Before a spec' drops methodology containing: {missing}"
    )


def test_grok_skill_bans_untrusted_input_topics():
    text = pool_skill_path("grok").read_text(encoding="utf-8").lower()
    assert "injection" in text, "grok skill lost the injection ban"
    assert "untrusted" in text, "grok skill lost the untrusted-input ban"


def test_skills_carry_no_home_directory_paths():
    offenders = []
    for path in sorted(SKILLS.rglob("SKILL.md")):
        text = path.read_text(encoding="utf-8")
        for marker in ("/home/", "/Users/"):
            if marker in text:
                offenders.append(f"{path.relative_to(ROOT)}: {marker}")
    assert not offenders, (
        "product-specific paths in shipped skills:\n" + "\n".join(offenders)
    )
