"""Derive skill sections from docs/knowledge.

Each skill marks a section ``<!-- derived: file.md#slug -->`` … ``<!--
/derived -->``. The body between the markers is the heading's body in
that knowledge file, verbatim. ``bin/foreman-skills-derive`` rewrites
every marked section; ``--check`` exits 1 on the first disagreement.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: (skill directory name, knowledge file, heading slug)
REQUIRED: tuple[tuple[str, str, str], ...] = (
    ("foreman-pool-grok", "models.md", "grok-4.6"),
    ("foreman-pool-claude", "models.md", "opus-5"),
    ("foreman-pool-codex", "models.md", "astra-6"),
    ("foreman-pool-muse", "models.md", "muse"),
    ("foreman-supervise", "team-calibration.md", "signals-during-a-front"),
    ("foreman-supervise", "verification.md", "verification"),
    ("foreman-orchestrate", "team-calibration.md", "team-calibration"),
    ("foreman-orchestrate", "team-calibration.md", "the-count"),
    ("foreman-orchestrate", "team-calibration.md", "the-model"),
    ("foreman-plan", "team-calibration.md", "team-calibration"),
)

OPEN_RE = re.compile(
    r"<!--\s*derived:\s*([^#\s]+)#(\S+?)\s*-->"
)
CLOSE = "<!-- /derived -->"
HEADING_RE = re.compile(r"^(#{1,2}) (.+)$")
SLUG_CUTS = (" (", " —", ": ", ",")
DATE_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")
RULING_RE = re.compile(r"\brul-[a-z0-9]+\b", re.I)


@dataclass(frozen=True)
class Section:
    """One knowledge heading and the body under it."""

    heading: str
    body: str
    level: int


@dataclass(frozen=True)
class Region:
    """One derived span inside a skill file."""

    skill: str
    source_file: str
    slug: str
    inner: str
    open_start: int
    inner_start: int
    inner_end: int
    close_end: int


@dataclass(frozen=True)
class Disagreement:
    """A skill section that does not match its knowledge heading."""

    skill: str
    source: str
    heading: str
    detail: str

    def message(self) -> str:
        return (
            f"{self.skill} disagrees on {self.heading} ({self.source}): "
            f"{self.detail}"
        )


def default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def heading_slug(title: str) -> str:
    """Stable slug: cut at the earliest separator, keep dots."""
    cuts = [title.find(sep) for sep in SLUG_CUTS if sep in title]
    if cuts:
        title = title[: min(cuts)]
    slug = re.sub(r"[^a-z0-9.]+", "-", title.lower().strip())
    return slug.strip("-")


def parse_sections(text: str) -> dict[str, Section]:
    """H1/H2 headings in a knowledge file, keyed by slug.

    The H1 body is the preamble before the first H2 (the document
    heading itself is not part of that body). An H2 body is the lines
    under that heading, not including the heading line.
    """
    lines = text.splitlines(keepends=True)
    current_slug: str | None = None
    current_heading: str | None = None
    current_level: int | None = None
    buf: list[str] = []
    out: dict[str, Section] = {}

    def flush() -> None:
        if current_slug is None or current_heading is None \
                or current_level is None:
            return
        if current_slug in out:
            raise ValueError(
                f"duplicate heading slug {current_slug!r} "
                f"({current_heading!r} and {out[current_slug].heading!r})"
            )
        raw = "".join(buf)
        if raw.startswith("\r\n"):
            raw = raw[2:]
        elif raw.startswith("\n"):
            raw = raw[1:]
        body = raw.rstrip() + ("\n" if raw.strip() else "")
        out[current_slug] = Section(
            current_heading, body, current_level)

    for line in lines:
        match = HEADING_RE.match(line.rstrip("\r\n"))
        if match:
            flush()
            current_level = len(match.group(1))
            current_heading = match.group(2).strip()
            current_slug = heading_slug(current_heading)
            buf = []
            continue
        if current_slug is not None:
            buf.append(line)
    flush()
    return out


def knowledge_dir(root: Path) -> Path:
    return root / "docs" / "knowledge"


def skills_dir(root: Path) -> Path:
    return root / "skills"


def load_knowledge(root: Path) -> dict[str, dict[str, Section]]:
    files: dict[str, dict[str, Section]] = {}
    directory = knowledge_dir(root)
    if not directory.is_dir():
        raise FileNotFoundError(f"no knowledge directory at {directory}")
    for path in sorted(directory.glob("*.md")):
        files[path.name] = parse_sections(path.read_text(encoding="utf-8"))
    return files


def iter_regions(skill: str, text: str) -> list[Region]:
    regions: list[Region] = []
    pos = 0
    while True:
        match = OPEN_RE.search(text, pos)
        if not match:
            break
        close = text.find(CLOSE, match.end())
        if close < 0:
            raise ValueError(
                f"{skill}: unmatched derived marker for "
                f"{match.group(1)}#{match.group(2)}"
            )
        regions.append(Region(
            skill=skill,
            source_file=match.group(1),
            slug=match.group(2),
            inner=text[match.end():close],
            open_start=match.start(),
            inner_start=match.end(),
            inner_end=close,
            close_end=close + len(CLOSE),
        ))
        pos = close + len(CLOSE)
    return regions


def canonical(text: str) -> str:
    stripped = text.strip("\n")
    return stripped + "\n" if stripped else ""


def skill_paths(root: Path) -> list[Path]:
    return sorted(skills_dir(root).glob("*/SKILL.md"))


def lookup(knowledge: dict[str, dict[str, Section]],
           source_file: str, slug: str) -> Section | None:
    return knowledge.get(source_file, {}).get(slug)


def disagreements(root: Path) -> list[Disagreement]:
    """Every derived section that does not match its knowledge heading.

    Required markers that are missing come first, in REQUIRED order, then
    any other marked section whose body differs, in skill-path order.
    """
    knowledge = load_knowledge(root)
    found: dict[tuple[str, str, str], Region] = {}
    extras: list[Disagreement] = []
    for path in skill_paths(root):
        skill = path.parent.name
        text = path.read_text(encoding="utf-8")
        for region in iter_regions(skill, text):
            key = (skill, region.source_file, region.slug)
            found[key] = region
            section = lookup(knowledge, region.source_file, region.slug)
            if section is None:
                extras.append(Disagreement(
                    skill, f"{region.source_file}#{region.slug}",
                    region.slug, "unknown heading"))
                continue
            if canonical(region.inner) != canonical(section.body):
                extras.append(Disagreement(
                    skill, f"{region.source_file}#{region.slug}",
                    section.heading, "bodies differ"))

    out: list[Disagreement] = []
    seen: set[tuple[str, str, str]] = set()
    for skill, source_file, slug in REQUIRED:
        key = (skill, source_file, slug)
        seen.add(key)
        section = lookup(knowledge, source_file, slug)
        heading = section.heading if section is not None else slug
        source = f"{source_file}#{slug}"
        if section is None:
            out.append(Disagreement(
                skill, source, heading, "unknown heading"))
            continue
        region = found.get(key)
        if region is None:
            out.append(Disagreement(
                skill, source, heading, "missing derived section"))
            continue
        if canonical(region.inner) != canonical(section.body):
            out.append(Disagreement(
                skill, source, heading, "bodies differ"))
    for item in extras:
        key = (item.skill, item.source.split("#", 1)[0],
               item.source.split("#", 1)[1])
        if key not in seen:
            out.append(item)
    return out


def rewrite_text(text: str, skill: str,
                 knowledge: dict[str, dict[str, Section]]) -> str:
    regions = iter_regions(skill, text)
    for region in reversed(regions):
        section = lookup(knowledge, region.source_file, region.slug)
        if section is None:
            raise ValueError(
                f"{skill}: unknown heading {region.source_file}#"
                f"{region.slug}"
            )
        body = canonical(section.body)
        text = text[:region.inner_start] + "\n" + body + text[region.inner_end:]
    return text


def derive(root: Path) -> list[Path]:
    """Rewrite every marked section. Returns paths whose bytes changed."""
    knowledge = load_knowledge(root)
    changed: list[Path] = []
    for path in skill_paths(root):
        original = path.read_text(encoding="utf-8")
        updated = rewrite_text(original, path.parent.name, knowledge)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(path)
    return changed


def after_header(text: str) -> list[tuple[int, str]]:
    """Lines after the H1 header, with 1-based line numbers."""
    lines = text.splitlines()
    if not lines:
        return []
    start = 1
    # The header is the H1 and the blank line that follows it, if any.
    if lines[0].startswith("# "):
        start = 1
        if len(lines) > 1 and lines[1] == "":
            start = 2
    numbered = [(i, line) for i, line in enumerate(lines, start=1)]
    return numbered[start:]


def amendment_offenders(text: str) -> list[tuple[int, str]]:
    """Dated amendment lines that do not carry a ``rul-`` id.

    The README's rule: amend a knowledge file with a dated line and the
    ruling id, never silently. Original section bodies are not dated
    lines; only a line that opens with an ISO date is an amendment.
    """
    bad: list[tuple[int, str]] = []
    for lineno, line in after_header(text):
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:]
        if not DATE_LINE_RE.match(stripped):
            continue
        if not RULING_RE.search(stripped):
            bad.append((lineno, line))
    return bad


def knowledge_amendment_offenders(root: Path) -> list[str]:
    """Human lines naming every offending amendment in docs/knowledge."""
    reports: list[str] = []
    directory = knowledge_dir(root)
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in amendment_offenders(text):
            reports.append(f"{path.name}:{lineno}: {line}")
    return reports


def rel(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="foreman-skills-derive",
        description=(
            "Rewrite every skill derived section from docs/knowledge. "
            "With --check, exit 1 naming the first disagreement."
        ),
    )
    parser.add_argument(
        "--check", action="store_true",
        help="do not write; exit 1 on the first disagreement",
    )
    args = parser.parse_args(argv)
    base = root if root is not None else default_root()
    try:
        found = disagreements(base)
    except (OSError, ValueError) as exc:
        print(f"foreman-skills-derive: {exc}", file=sys.stderr)
        return 2
    if args.check:
        if found:
            print(found[0].message(), file=sys.stderr)
            return 1
        n = sum(len(iter_regions(path.parent.name,
                                 path.read_text(encoding="utf-8")))
                for path in skill_paths(base))
        print(f"ok: {n} derived section(s) agree")
        return 0
    try:
        changed = derive(base)
    except (OSError, ValueError) as exc:
        print(f"foreman-skills-derive: {exc}", file=sys.stderr)
        return 2
    if not changed:
        print("ok: already derived")
        return 0
    for path in changed:
        print(rel(base, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
