"""The planner's skill: examples that pass, rules that match the validator.

Each test drives the real ``front add --dry-run`` path against the example
briefs shipped beside the skill — the break they catch is a skill edit that
deletes a section or an example edit that stops validating. The content
tests pin the skill's wording to the validator's rules in
``src/foreman/fronts.py`` and the must-contain list in ``docs/DESIGN.md``
section 15, so dropping either is red, not drift.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from foreman import fronts
from foreman.caller import SESSION_ENV

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "foreman-plan" / "SKILL.md"
EXAMPLES = ROOT / "skills" / "foreman-plan" / "examples"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    subprocess.run(["git", "init", "-b", "main", "-q", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c",
                    "user.email=test@example.invalid", "-c",
                    "user.name=foreman-test", "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True)
    return tmp_path


def dry_run_copy(env: Path, example: Path, capsys) -> None:
    """The example brief, copied into a throwaway repo, must dry-run clean.

    The copy keeps the test hermetic: the example directory's own repo is
    incidental, while the brief content is what must validate.
    """
    target = env / example.name
    shutil.copytree(example, target)
    assert fronts.front_add_main(str(target), dry_run=True) == 0
    out, err = capsys.readouterr()
    assert "would write" in out
    assert err == ""


def test_single_task_example_passes_dry_run(env, capsys):
    dry_run_copy(env, EXAMPLES / "single-task", capsys)


def test_multi_task_example_passes_dry_run(env, capsys):
    dry_run_copy(env, EXAMPLES / "multi-task", capsys)


def test_multi_task_example_chains_after_monitors_and_rules():
    import tomllib

    data = tomllib.loads(
        (EXAMPLES / "multi-task" / "brief.toml").read_bytes().decode("utf-8"))
    titles = [task["title"] for task in data["task"]]
    assert len(titles) >= 2
    assert any(task.get("after") for task in data["task"]), \
        "multi-task example needs an `after` chain"
    chained = {name for task in data["task"] for name in task.get("after", [])}
    assert chained <= set(titles)
    assert len(data.get("monitor", [])) >= 1
    assert len(data.get("rule", [])) >= 1


#: One anchor per DESIGN section 4.3 validation item: the wording the skill
#: uses where the validator would refuse. The break: a skill edit that drops
#: a rule the code enforces, leaving the planner to guess it.
VALIDATION_ANCHORS = [
    "OUT OF SCOPE",                    # scope carries all four headings
    "non-empty `verify` command",      # every task has verify
    "positive integer",                # every task has size
    "names only opus, muse, astra, grok",  # allocation roles exist
    "fronts already on the ledger",    # front after names real fronts
    "titles in this brief",            # task after names real tasks
    "graph has no cycle",              # task after has no cycle
    "`measure`, `unit`, `every`",      # every monitor has all three
    "operator plus number",            # alert parses
    "one sentence on one line",        # done-when is one sentence
    "directory-safe and unique",       # name is unique
    "branch that exists",              # land-on is a branch that exists
]


def test_skill_names_every_validation_item():
    text = SKILL.read_text(encoding="utf-8")
    for anchor in VALIDATION_ANCHORS:
        assert anchor in text, f"skill lost validation item {anchor!r}"


#: One anchor per DESIGN section 15 must-contain item for foreman-plan, plus
#: the completion criterion the job spec requires the skill to state.
SECTION_15_ANCHORS = [
    "owner → planner → foreman → supervisor → worker",  # the world
    "project > front > task > job",                    # the hierarchy
    "the brief file",                                  # schema and validation
    "Blunt",                                           # sharp scope, both sides
    "one owner's question answered by one number",     # a good monitor
    "grok",                                            # the pool roles
    "Interview order",                                 # the interview order
    "Pre-handover checklist",                          # when a brief is ready
    "Hand over the directory path",                    # how to hand over
    "--dry-run` exits 0",                              # completion criterion
]


def test_skill_names_every_section_15_item():
    text = SKILL.read_text(encoding="utf-8")
    for anchor in SECTION_15_ANCHORS:
        assert anchor in text, f"skill lost section-15 item {anchor!r}"


def test_skill_has_frontmatter_name_and_description():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---\n", 2)[1]
    assert "name: foreman-plan" in frontmatter
    assert "description:" in frontmatter


def brief_keys(path: Path) -> set[str]:
    """Every key and table name an example brief actually uses.

    Read from the file's own text rather than from a parsed table, so a
    table header (``[[task]]``) counts as the token the skill has to
    teach, exactly as the planner will type it.
    """
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            keys.add(line)
        elif "=" in line and not line.startswith("#"):
            keys.add(line.split("=", 1)[0].strip())
    return keys


def test_skill_names_every_field_the_examples_use():
    """The skill teaches the validator's field names, not synonyms.

    Prose anchors alone let the skill rename a field the planner must
    type — ``done-when`` to ``finished-when``, say — and stay green while
    teaching a brief ``front add`` refuses. Deriving the list from the
    examples means a field can only be renamed in both places at once,
    and the examples are dry-run against the real validator.
    """
    text = SKILL.read_text(encoding="utf-8")
    used: set[str] = set()
    for example in sorted(EXAMPLES.iterdir()):
        used |= brief_keys(example / "brief.toml")
    assert used, "no example briefs to derive field names from"
    missing = sorted(key for key in used if key not in text)
    assert not missing, f"skill never names {missing}"
