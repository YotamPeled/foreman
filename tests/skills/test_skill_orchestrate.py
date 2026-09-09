"""The foreman's and the merge desk's skills: procedure the design requires.

The break each test catches: a skill edit that drops a routing kind, a
never-does, a cadence checklist, or a desk verb, leaving its reader to
guess or to call a verb its role is refused. Expected values are read
from the code that enforces them — ``foreman.entities`` for the inbox
kinds, ``foreman.launch`` for the desk's verbs and the CLI parser for
checklist verbs — never hard-coded: a test that hard-codes the verb list
passes while the desk ships a fourth verb with no skill behind it.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

from foreman import entities
from foreman import launch

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
ORCHESTRATE = SKILLS / "foreman-orchestrate" / "SKILL.md"
MERGE = SKILLS / "foreman-merge" / "SKILL.md"

#: Every ``merge <word>`` the merge skill utters as a verb. Words outside
#: this set (queue, ledger, desk) are prose, not verbs, and are not matched.
#: A merge verb as the skill writes one: in backticks, so prose about
#: merging is not read as a verb. Open on the verb word on purpose —
#: a closed alternation can only ever find verbs that exist, which
#: makes the "and no other" half of the test below unfalsifiable.
MERGE_VERB_RE = re.compile(r"`merge\s+([a-z][a-z-]*)`")

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


#: Backtick-quoted ``foreman …`` mentions. Prose about a verb is not a
#: mention: the merge skill's test uses the same rule, so a sentence
#: that talks about launching is not read as ``foreman launch``.
FOREMAN_CMD_RE = re.compile(r"`foreman\s+([^`]+)`")

CHECKLIST_HEADINGS = ("STARTUP", "PER LANDING", "PER SHIFT")

#: Distinctive phrases each checklist must keep, one per item the brief
#: named. The break: a heading survives while an item is paraphrased
#: away, and the next foreman skips the step that cost a night.
CHECKLIST_PHRASES = {
    "STARTUP": [
        "collector is active",
        "FOREMAN_SESSION",
        "merge desk",
        "swarm rulings",
        "observed",
    ],
    "PER LANDING": [
        "fresh worktree",
        "secrets",
        "merge land",
        "reinstall",
        "--merged",
        "collector restart",
        "--dry-run",
        "ledger",
        "orchestrator",
    ],
    "PER SHIFT": [
        "checkpoint",
        "foreman-status",
        "owner's cap",
        "only for sessions you launched",
    ],
}


def _h2_section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    lines = text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines)
                 if line.startswith(marker))
    end = next((i for i, line in enumerate(lines[start + 1:], start + 1)
                if line.startswith("## ")), len(lines))
    return "".join(lines[start:end])


def _verb_index() -> dict[str, tuple[set[str], set[str],
                                     list[tuple[set[str] | None, bool]]]]:
    """Every registered verb path → (flags, valued flags, positionals).

    Read off ``launch.registered_verbs`` — the same walk the role prompt
    uses — so a renamed verb or flag turns this test red. A positional is
    ``(choices or None, greedy)``: None means the parser takes a freeform
    argument at that slot. Nested verb groups (``front``) are indexed too,
    with their subcommand names as the first positional's choices, so a
    wrong subcommand names the token and the group rather than claiming
    the group is not a verb.
    """
    launch.import_verb_modules()
    index: dict[str, tuple[set[str], set[str],
                           list[tuple[set[str] | None, bool]]]] = {}
    for path, parser, _help in launch.registered_verbs():
        flags: set[str] = set()
        valued: set[str] = set()
        positionals: list[tuple[set[str] | None, bool]] = []
        for action in parser._actions:
            if action.option_strings:
                for opt in action.option_strings:
                    flags.add(opt)
                    if action.nargs != 0:
                        valued.add(opt)
                continue
            if isinstance(action, argparse._SubParsersAction):
                continue
            choices = set(action.choices) if action.choices else None
            greedy = action.nargs in ("*", "+")
            positionals.append((choices, greedy))
        index[path] = (flags, valued, positionals)
    children: dict[str, set[str]] = {}
    for path in list(index):
        parts = path.split()
        for i in range(1, len(parts)):
            parent = " ".join(parts[:i])
            children.setdefault(parent, set()).add(parts[i])
    for parent, names in children.items():
        if parent not in index:
            index[parent] = (set(), set(), [(names, False)])
    return index


def _named_commands(section: str) -> list[str]:
    return [match.strip() for match in FOREMAN_CMD_RE.findall(section)]


def _longest_verb(tokens: list[str], index: dict) -> str | None:
    for length in range(len(tokens), 0, -1):
        candidate = " ".join(tokens[:length])
        if candidate in index:
            return candidate
    return None


def _unresolved(command: str, index: dict) -> str | None:
    """None if ``command`` walks the parser; otherwise the token that does not.

    Tokens after the verb path are an option the parser names, a
    positional choice it names at that slot, or a freeform argument
    where the parser takes one. A leftover token that is none of those
    is the stale skill. The message names the token and the verb.
    """
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"{command!r} does not parse ({exc})"
    if not tokens:
        return "empty command"
    path = _longest_verb(tokens, index)
    if path is None:
        return f"{tokens[0]!r} is not a CLI verb"
    consumed = len(path.split())
    flags, valued, positionals = index[path]
    pos_i = 0
    skip_value = False
    for token in tokens[consumed:]:
        if skip_value:
            skip_value = False
            continue
        if token.startswith("-"):
            flag = token.split("=", 1)[0]
            if flag not in flags:
                return f"{flag} is not a flag on {path!r}"
            if "=" not in token and flag in valued:
                skip_value = True
            continue
        if pos_i >= len(positionals):
            return f"{token!r} is not a token on {path!r}"
        choices, greedy = positionals[pos_i]
        if choices is not None and token not in choices:
            return f"{token!r} is not a token on {path!r}"
        if not greedy:
            pos_i += 1
    return None


def _step_text(section: str) -> str:
    """Numbered steps of a checklist section, excluding the summary.

    The break: a Done-when line repeats a deleted step's words and the
    phrase check stays green.
    """
    lines: list[str] = []
    in_step = False
    for line in section.splitlines():
        if re.match(r"^\d+\.\s", line):
            in_step = True
            lines.append(line)
        elif in_step and line[:1] in " \t":
            lines.append(line)
        else:
            in_step = False
    return " ".join(" ".join(lines).split())


def test_orchestrate_carries_startup_landing_and_shift_checklists():
    """The three cadences, each item still named, every verb still shipped.

    The break: STARTUP is dropped, or a checklist names ``foreman foo``
    after foo was renamed, and the next foreman rebuilds the routine
    from a scratch ledger. Expected verbs come from the parser, never
    from a hand-written list.
    """
    text = ORCHESTRATE.read_text(encoding="utf-8")
    missing_headings = [heading for heading in CHECKLIST_HEADINGS
                        if f"## {heading}" not in text]
    assert not missing_headings, (
        f"orchestrate skill drops checklists: {missing_headings}"
    )
    index = _verb_index()
    assert index, "the CLI registered no verbs to resolve against"
    stale: list[str] = []
    empty: list[str] = []
    dropped: list[str] = []
    for heading in CHECKLIST_HEADINGS:
        section = _h2_section(text, heading)
        commands = _named_commands(section)
        if not commands:
            empty.append(heading)
        for command in commands:
            problem = _unresolved(command, index)
            if problem:
                stale.append(f"{heading}: `foreman {command}` ({problem})")
        lowered = _step_text(section).lower()
        for phrase in CHECKLIST_PHRASES[heading]:
            if phrase.lower() not in lowered:
                dropped.append(f"{heading}: {phrase!r}")
    assert not empty, (
        f"checklist names no `foreman <verb>`: {empty}"
    )
    assert not stale, (
        "checklist names verbs the CLI does not have:\n  " + "\n  ".join(stale)
    )
    assert not dropped, (
        f"checklist drops items containing: {dropped}"
    )


def test_checklist_rejects_a_subcommand_the_parser_does_not_have():
    """A skill that names collector reboot is the stale skill.

    The break: reboot is not a collector action, and the old resolver
    returned None because leftover tokens that were not flags and not
    choices fell out of the loop.
    """
    fixture = (
        "## STARTUP\n"
        "1. A stale collector prints `foreman collector reboot` as the fix.\n"
    )
    index = _verb_index()
    commands = _named_commands(fixture)
    assert commands == ["collector reboot"]
    problem = _unresolved(commands[0], index)
    assert problem is not None, "reboot must fail to resolve"
    assert "reboot" in problem
    assert "collector" in problem

    # A verb that takes no positional at all rejects a token just the
    # same: the other way past the resolver is a token beyond the
    # positionals the parser declares.
    beyond = _unresolved("doctor extra", index)
    assert beyond is not None, "a token past the verb's positionals must fail"
    assert "extra" in beyond
    assert "doctor" in beyond


def test_unresolved_accepts_a_positional_choice_and_a_freeform_argument():
    """A real choice and a freeform slot both still resolve.

    The break: rejecting every leftover token, including a session id
    or a ``<sha>`` placeholder, because it is not in the choice set.
    """
    index = _verb_index()
    assert _unresolved("collector restart", index) is None
    assert _unresolved("relaunch <session>", index) is None
    assert _unresolved("front close <front> --merged <sha>", index) is None


def test_unresolved_names_the_subcommand_on_a_verb_group():
    """A wrong nested command names the token, not the group.

    The break: ``front nosuch`` reports that ``front`` is not a CLI
    verb, when ``front`` is a real group and ``nosuch`` is the miss.
    """
    index = _verb_index()
    problem = _unresolved("front nosuch", index)
    assert problem is not None
    assert "nosuch" in problem
    assert "front" in problem
    assert "not a CLI verb" not in problem


def test_phrase_check_reads_numbered_steps_not_the_summary():
    """Deleting a step turns the phrase check red even if Done-when keeps the word.

    The break: the phrase list is matched against the whole section, and
    ``reinstalled`` in the summary still contains ``reinstall``.
    """
    fixture = (
        "## PER LANDING\n"
        "\n"
        "1. Carry on.\n"
        "\n"
        "Done when: the main-only checkout is reinstalled.\n"
    )
    lowered = _step_text(fixture).lower()
    assert "reinstall" not in lowered


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
