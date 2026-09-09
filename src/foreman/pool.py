"""`foreman pool list|add|remove|clone`: the owner's verbs over pool plugins.

A pool is a directory (see :mod:`foreman.pools.plugins`). The packaged
ones ship under the package; ``clone`` copies one into the user's pools
directory so it can be edited, and ``remove`` deletes the user's copy —
removing a clone falls back to the packaged pool rather than leaving a
hole, because the packaged directory is still there. ``add`` scaffolds
a new user pool (or installs a directory with ``--from``); ``list``
prints every pool the launcher can resolve and where each came from.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from . import caller, cli
from .caller import Refusal
from .pools import names as pool_names
from .pools import plugins
from .pools.plugins import POOL_NAME_RE

#: What `pool add` scaffolds when no `--from` directory is given: a
#: valid, listable pool with no implementation yet. A launch of it is
#: refused with the reason instead of crashing.
SCAFFOLD_MANIFEST = """\
# Pool `{name}` (user pool, scaffolded by `foreman pool add`).
#
# Identity: the pool answers to the directory's name. Behaviour: add a
# `[vendor]` table declaring the command that runs one worker, e.g.
#
#   [vendor]
#   argv = ["my-vendor", "exec", "--prompt-file", "{{job_path}}"]
#   stdin = "none"
#
# `{placeholders}` in argv are replaced per launch: session_id, model,
# effort, worktree, job_path, role_path, log_path, pid_path,
# verdict_path, scratch_dir, branch, target, timeout. `stdin = "job"`
# redirects the job file into the command's standard input. `adapter`
# (instead of `[vendor]`) delegates every piece to a packaged adapter.

name = "{name}"
model = "{name}"
timeout_default = "20m"
interactive = false
roles = []
"""

#: The skill every pool directory carries; the scaffold's is a stub.
SCAFFOLD_SKILL = """\
# Skill: `foreman-pool-{name}`

What pool `{name}` is for, and how a supervisor writes a spec it does
well. Fill this in when the pool's vendor command is defined: unit
size, effort, timeout, what it cannot take, how to read its output.
"""


def _refuse(violations: list[str]) -> int:
    return Refusal(violations).report()


def _check_name(name: str | None, violations: list[str]) -> str | None:
    if not (isinstance(name, str) and name.strip()):
        violations.append("field 'name' is required")
        return None
    name = name.strip()
    if not POOL_NAME_RE.fullmatch(name):
        violations.append(
            f"field 'name' must look like a pool name "
            f"(letters, digits, dash, under; got '{name}')")
        return None
    return name


def pool_list_main() -> int:
    me, violations = caller.resolve("pool list")
    caller.check_role(me, "pool list", violations=violations)
    if violations:
        return _refuse(violations)
    for name in pool_names():
        manifest, source = plugins.describe(name)
        if manifest is not None:
            roles = ", ".join(manifest.roles) or "-"
            print(f"{manifest.name} {manifest.model} [{source}] "
                  f"roles: {roles} timeout: {manifest.timeout_default}")
        else:
            print(f"{name} [{source}]")
    return 0


def pool_clone_main(name: str | None) -> int:
    me, violations = caller.resolve("pool clone")
    caller.check_role(me, "pool clone", violations=violations)
    name = _check_name(name, violations)
    packaged = plugins.packaged_dir(name) if name else None
    dest = plugins.user_dir(name) if name else None
    if name is not None:
        if packaged is None or not (packaged / plugins.MANIFEST_FILENAME).is_file():
            known = ", ".join(p for p in pool_names()
                              if (plugins.packaged_dir(p)
                                  / plugins.MANIFEST_FILENAME).is_file())
            violations.append(
                f"unknown packaged pool '{name}'; packaged pools: "
                + (known or "(none)"))
        elif dest is not None and dest.exists():
            violations.append(
                f"user pool '{name}' already exists at {dest}; "
                f"remove it first to re-clone")
    if violations or packaged is None or dest is None:
        return _refuse(violations)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(packaged, dest)
    except OSError as exc:
        return _refuse(
            [f"cannot clone pool '{name}' to {dest}: {exc.strerror or exc}"])
    print(f"{name} cloned to {dest}")
    return 0


def pool_remove_main(name: str | None) -> int:
    me, violations = caller.resolve("pool remove")
    caller.check_role(me, "pool remove", violations=violations)
    name = _check_name(name, violations)
    dest = plugins.user_dir(name) if name else None
    if name is not None and dest is not None and not dest.is_dir():
        violations.append(
            f"no user pool '{name}' at {dest} (nothing to remove)")
    if violations or name is None or dest is None:
        return _refuse(violations)
    try:
        shutil.rmtree(dest)
    except OSError as exc:
        return _refuse(
            [f"cannot remove user pool '{name}' at {dest}: "
             f"{exc.strerror or exc}"])
    if (plugins.packaged_dir(name)
            / plugins.MANIFEST_FILENAME).is_file():
        print(f"removed user pool '{name}'; packaged '{name}' answers again")
    else:
        print(f"removed user pool '{name}'")
    return 0


def pool_add_main(name: str | None, from_dir: str | None = None) -> int:
    me, violations = caller.resolve("pool add")
    caller.check_role(me, "pool add", violations=violations)
    name = _check_name(name, violations)
    dest = plugins.user_dir(name) if name else None
    source: Path | None = None
    if name is not None and dest is not None and dest.exists():
        violations.append(
            f"pool '{name}' already exists at {dest}; "
            f"remove it first to replace it")
    if from_dir is not None:
        source = Path(from_dir)
        if not source.is_dir():
            violations.append(
                f"field 'from' is not a directory (got '{from_dir}')")
            source = None
        elif not (source / plugins.MANIFEST_FILENAME).is_file():
            violations.append(
                f"field 'from' holds no pool (no {plugins.MANIFEST_FILENAME} "
                f"in '{from_dir}')")
            source = None
        elif name is not None:
            try:
                manifest = plugins.load_manifest(source)
            except plugins.InvalidManifest as exc:
                violations.append(
                    f"field 'from' holds no pool ({exc})")
            else:
                if manifest.name != name:
                    violations.append(
                        f"field 'from' holds pool '{manifest.name}', "
                        f"not '{name}'")
    if violations or name is None or dest is None:
        return _refuse(violations)
    try:
        if source is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, dest)
        else:
            dest.mkdir(parents=True, exist_ok=False)
            (dest / plugins.MANIFEST_FILENAME).write_text(
                SCAFFOLD_MANIFEST.format(name=name,
                                         placeholders="{placeholders}"),
                encoding="utf-8")
            (dest / plugins.SKILL_FILENAME).write_text(
                SCAFFOLD_SKILL.format(name=name), encoding="utf-8")
    except OSError as exc:
        return _refuse(
            [f"cannot add pool '{name}' at {dest}: {exc.strerror or exc}"])
    print(f"{name} added at {dest}")
    return 0


def add_pool_arguments(sub: argparse.ArgumentParser) -> None:
    verbs = sub.add_subparsers(dest="pool_verb", required=True)
    verbs.add_parser("list", help="Print every pool and where it came from.")
    clone = verbs.add_parser(
        "clone", help="Copy a packaged pool into the user pools directory.")
    clone.add_argument("name", help="packaged pool to clone")
    remove = verbs.add_parser(
        "remove", help="Delete a user pool (a clone falls back to packaged).")
    remove.add_argument("name", help="user pool to remove")
    add = verbs.add_parser(
        "add", help="Scaffold a new user pool, or install one with --from.")
    add.add_argument("name", help="pool to add")
    add.add_argument("--from", dest="from_dir", default=None,
                     help="directory holding the pool to install")


@cli.subcommand("pool", help="List, add, clone or remove a pool.")
def _pool_entry(args: argparse.Namespace) -> int:
    if args.pool_verb == "list":
        return pool_list_main()
    if args.pool_verb == "clone":
        return pool_clone_main(args.name)
    if args.pool_verb == "remove":
        return pool_remove_main(args.name)
    if args.pool_verb == "add":
        return pool_add_main(args.name, from_dir=args.from_dir)
    raise AssertionError(f"unknown pool verb {args.pool_verb!r}")


_pool_entry.add_arguments = add_pool_arguments  # type: ignore[attr-defined]
