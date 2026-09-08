"""The foreman's and the merge desk's skills: procedure the design requires.

The break each test catches: a skill edit that drops a routing kind, a
never-does, or a desk verb, leaving its reader to guess or to call a verb
its role is refused. Expected values are read from the code that enforces
them — ``foreman.entities`` for the inbox kinds, ``foreman.launch`` for the
desk's verbs — never hard-coded: a test that hard-codes the verb list passes
while the desk ships a fourth verb with no skill behind it.
"""

from __future__ import annotations

import re
from pathlib import Path

from foreman import entities
from foreman import launch

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
ORCHESTRATE = SKILLS / "foreman-orchestrate" / "SKILL.md"
MERGE = SKILLS / "foreman-merge" / "SKILL.md"

#: Every ``merge <word>`` the merge skill utters as a verb. Words outside
#: this set (queue, ledger, desk) are prose, not verbs, and are not matched.
MERGE_VERB_RE = re.compile(r"merge\s+(take|land|fail|request)")

#: The three things DESIGN.md section 3 says the foreman never does. The
#: break: the skill paraphrases one away until a foreman dispatches work.
NEVER_DOES = [
    "dispatch a job",  # never dispatches: jobs belong to supervisors
    "write a task",  # never writes: tasks come from the brief
    "answer for the owner",  # never answers on the reserved kinds
]


def desk_merge_verbs() -> set[str]:
    """Every ``merge …`` verb the CLI registers for the desk role.

    Read off ``launch.desk_verbs`` — the same registry the role prompt
    renders — so a verb the desk gains or loses turns this test red.
    """
    return {verb for verb, _line in launch.desk_verbs()
            if verb.startswith("merge ")}


def test_both_skills_exist_with_frontmatter():
    for path, name in ((ORCHESTRATE, "foreman-orchestrate"),
                       (MERGE, "foreman-merge")):
        assert path.is_file(), f"missing skill at {path}"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{name} lost its frontmatter"
        frontmatter = text.split("---\n", 2)[1]
        assert f"name: {name}" in frontmatter, \
            f"{name} frontmatter names no skill"
        assert "description:" in frontmatter, \
            f"{name} frontmatter carries no description"


def test_orchestrate_names_every_inbox_kind():
    kinds = entities.INBOX_KINDS
    assert len(kinds) == 4, \
        f"inbox kinds moved on ({kinds}); the skill test tracks four"
    text = ORCHESTRATE.read_text(encoding="utf-8").lower()
    missing = [kind for kind in kinds if kind.lower() not in text]
    assert not missing, (
        f"orchestrate skill drops inbox kinds: {missing} "
        f"(registry says {list(kinds)})"
    )


def test_orchestrate_names_all_three_never_does():
    text = ORCHESTRATE.read_text(encoding="utf-8").lower()
    missing = [phrase for phrase in NEVER_DOES
               if phrase.lower() not in text]
    assert not missing, (
        f"orchestrate skill drops never-does: {missing}"
    )


def test_orchestrate_names_admission_routing_and_digest():
    """Admission, routing, and digest anchors from DESIGN.md section 3."""
    text = ORCHESTRATE.read_text(encoding="utf-8").lower()
    anchors = [
        "after",  # admitted when its after fronts are done
        "prefer",  # queue is owner preference then plan order
        "ceiling",  # allocation is a ceiling, never a reservation
        "first come",  # pools are shared first come, first served
        "ruling",  # everything else answered and recorded as a ruling
        "digest",  # computed from ledgers plus judgement
        "judgement",  # the digest's paragraph of judgement
    ]
    missing = [anchor for anchor in anchors if anchor not in text]
    assert not missing, (
        f"orchestrate skill drops design anchors: {missing}"
    )


def test_merge_names_every_desk_verb_and_no_other():
    expected = desk_merge_verbs()
    assert expected, "registry gates no merge verb for the desk role"
    text = MERGE.read_text(encoding="utf-8")
    found = set(MERGE_VERB_RE.findall(text))
    found = {f"merge {verb}" for verb in found}
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    assert not missing, (
        f"merge skill drops desk verbs {missing} "
        f"(registry says {sorted(expected)})"
    )
    assert not extra, (
        f"merge skill names verbs the desk does not have: {extra} "
        f"(registry says {sorted(expected)})"
    )


def test_merge_names_landing_procedure_and_self_landing():
    """Rebase, check, fail-back, and self-landing anchors (§3, §11)."""
    text = MERGE.read_text(encoding="utf-8").lower()
    anchors = [
        "rebase",  # rebase onto the target
        "detach",  # landing in a detached worktree
        "[merge]",  # the target's check command from the configuration
        "--reason",  # fail carries a reason back to the supervisor
        "never",  # never a fix by the desk
        '"self"',  # merge = "self" is the default
        "task landed",  # self-landing fronts land by task landed
    ]
    missing = [anchor for anchor in anchors if anchor not in text]
    assert not missing, (
        f"merge skill drops landing anchors: {missing}"
    )


def test_orchestrate_and_merge_carry_no_home_directory_paths():
    offenders = []
    for path in (ORCHESTRATE, MERGE):
        text = path.read_text(encoding="utf-8")
        for marker in ("/home/", "/Users/"):
            if marker in text:
                offenders.append(f"{path.relative_to(ROOT)}: {marker}")
    assert not offenders, (
        "product-specific paths in shipped skills:\n" + "\n".join(offenders)
    )
