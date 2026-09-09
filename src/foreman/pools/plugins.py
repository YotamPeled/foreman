"""Pool plugin directories: packaged defaults, user overrides.

A pool is a directory holding ``manifest.toml`` (name, model,
timeout_default, interactive, the roles it serves, and either the
packaged ``adapter`` implementing it or a ``[vendor]`` command) plus a
``SKILL.md`` saying what the pool is for.

Packaged pools live under this package (``src/foreman/pools/<name>/``);
user pools live under the user pools directory
(:func:`foreman.paths.pool_dir`, one directory per pool name) and
replace the packaged pool of the same name entirely — no merging. A
user directory whose manifest is missing or does not validate falls
back to the packaged pool with one warning on stderr naming the
directory, never a crash.

The five pieces a pool must have resolve per pool as follows:

- ``launch``: the ``[vendor]`` argv when the manifest declares one,
  wrapped exactly the way the packaged adapters wrap theirs (pid file
  first, finish marker inside the redirected stream); otherwise the
  named packaged adapter's launch.
- ``observe``: the named adapter's reading, else the shared
  :func:`foreman.pools._common.observe_session` (the finish-marker
  line).
- ``meter``: nothing reports one yet, so this answers ``None`` the way
  the codex adapter's usage does — no number is invented.
- ``usage``: the named adapter's reading, else ``None``.
- ``verdict``: the named adapter's reading where it has one, else the
  shared :func:`foreman.pools._common.read_verdict`.

Only the packaged Python adapters and commands the user wrote in their
own pools directory are ever executed: a manifest carries data (an argv
list, a stdin choice), never code, and ``{placeholders}`` in a vendor
argv substitute a fixed set of launch paths — unknown braces pass
through untouched.
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import Popen

from . import LaunchContext, PoolAdapter
from . import _common
from .. import paths
from ..entities import Session

#: A pool directory holds this file, and the loader reads nothing else.
MANIFEST_FILENAME = "manifest.toml"
#: What the pool is for; shipped per packaged pool, cloned with it.
SKILL_FILENAME = "SKILL.md"
#: Pool names are directory names: start alnum, then alnum, dash, under.
POOL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
#: The vendor command's standard input: the job file, or no redirection.
VENDOR_STDINS = ("none", "job")
#: ``{names}`` a vendor argv may use; each is replaced with the launch's
#: value for it. Anything else in braces is left alone, so vendor syntax
#: using braces survives the substitution.
VENDOR_VARS = (
    "session_id", "model", "effort", "worktree", "job_path",
    "role_path", "log_path", "pid_path", "verdict_path",
    "scratch_dir", "branch", "target", "timeout",
)


class InvalidManifest(Exception):
    """A pool directory's manifest is missing or unusable: the reason."""


@dataclass(frozen=True)
class PoolManifest:
    """A validated pool manifest: identity plus one implementation."""

    name: str
    model: str
    timeout_default: str
    interactive: bool
    roles: tuple[str, ...] = ()
    #: The registered packaged adapter implementing this pool, if any.
    adapter: str | None = None
    #: A directory-declared vendor command, if any; wins over ``adapter``.
    vendor_argv: tuple[str, ...] | None = None
    vendor_stdin: str = "none"
    #: Where this manifest was read from.
    directory: Path | None = field(default=None, compare=False)


def packaged_dir(name: str) -> Path:
    """The shipped directory for pool ``name``."""
    return Path(__file__).with_name(name)


def user_dir(name: str) -> Path:
    """The user's directory for pool ``name`` (the override, or the pool)."""
    return paths.pool_dir(name)


def packaged_names() -> list[str]:
    """Every pool shipped under this package (a directory with a manifest)."""
    try:
        entries = sorted(Path(__file__).parent.iterdir())
    except OSError:
        return []
    return [entry.name for entry in entries
            if entry.is_dir() and (entry / MANIFEST_FILENAME).is_file()]


def user_names() -> list[str]:
    """Every pool directory the user holds (valid or not)."""
    root = paths.config_dir() / "pools"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [entry.name for entry in entries if entry.is_dir()]


def _invalid(reason: str) -> InvalidManifest:
    return InvalidManifest(reason)


def load_manifest(directory: Path | str) -> PoolManifest:
    """Read and validate a pool directory's manifest.

    Raises :class:`InvalidManifest` naming what is wrong: a missing
    file, an unparsable one, a name that is not the directory's, a
    missing or empty identity field, a timeout nothing parses, an
    ``adapter`` no adapter registers, or a ``[vendor]`` table with no
    usable argv. Unknown keys are ignored. A manifest naming neither an
    adapter nor a vendor command is valid but unlaunchable: it lists,
    and a launch of it is refused with the reason instead of crashing.
    """
    directory = Path(directory)
    path = directory / MANIFEST_FILENAME
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise _invalid(f"{path} is missing (a pool is a directory "
                       f"holding {MANIFEST_FILENAME})") from None
    except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
        raise _invalid(f"{path} does not parse ({exc})") from None
    if not isinstance(raw, dict):
        raise _invalid(f"{path} does not parse (not a TOML table)")
    name = raw.get("name")
    if not isinstance(name, str) or not POOL_NAME_RE.fullmatch(name):
        raise _invalid(f"{path} names no pool (field 'name' must look "
                       f"like a pool name, got {name!r})")
    if name != directory.name:
        raise _invalid(f"{path} names pool {name!r} but lives in "
                       f"directory {directory.name!r}")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise _invalid(f"{path} names no model (field 'model' must be "
                       f"a non-empty string)")
    timeout = raw.get("timeout_default")
    if not isinstance(timeout, str) or \
            _common.timeout_seconds(timeout) is None:
        raise _invalid(f"{path} names no timeout (field "
                       f"'timeout_default' must parse like '20m', "
                       f"got {timeout!r})")
    interactive = raw.get("interactive", False)
    if not isinstance(interactive, bool):
        raise _invalid(f"{path} misnames 'interactive' "
                       f"(must be true or false, got {interactive!r})")
    roles = raw.get("roles", [])
    if not isinstance(roles, list) or any(
            not isinstance(role, str) or not role for role in roles):
        raise _invalid(f"{path} misnames 'roles' (must be a list of "
                       f"role names, got {roles!r})")
    adapter = raw.get("adapter")
    if adapter is not None and not isinstance(adapter, str):
        raise _invalid(f"{path} misnames 'adapter' "
                       f"(must be an adapter name, got {adapter!r})")
    if isinstance(adapter, str):
        from . import REGISTRY

        if adapter not in REGISTRY:
            raise _invalid(f"{path} names adapter {adapter!r}, which no "
                           f"pool registers")
    vendor_argv: tuple[str, ...] | None = None
    vendor_stdin = "none"
    vendor = raw.get("vendor")
    if vendor is not None:
        if not isinstance(vendor, dict):
            raise _invalid(f"{path} misnames 'vendor' (must be a table "
                           f"with 'argv', got {vendor!r})")
        argv = vendor.get("argv")
        if not isinstance(argv, list) or not argv or any(
                not isinstance(part, str) or not part for part in argv):
            raise _invalid(f"{path} misnames 'vendor.argv' (must be a "
                           f"non-empty list of command words)")
        vendor_argv = tuple(argv)
        stdin = vendor.get("stdin", "none")
        if stdin not in VENDOR_STDINS:
            raise _invalid(f"{path} misnames 'vendor.stdin' (must be one "
                           f"of {', '.join(VENDOR_STDINS)}, got {stdin!r})")
        vendor_stdin = stdin
    return PoolManifest(
        name=name, model=model, timeout_default=timeout,
        interactive=interactive, roles=tuple(roles),
        adapter=adapter, vendor_argv=vendor_argv,
        vendor_stdin=vendor_stdin, directory=directory,
    )


#: Directories already warned about in this process, so a screen reading
#: every pool still says it once. Keyed by directory: a test pointing
#: the config elsewhere is a different directory and warns again.
_WARNED: set[str] = set()


def warn_once(directory: Path | str, reason: object) -> None:
    """One stderr line naming a broken pool directory, once per process."""
    key = str(directory)
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(f"foreman: warning: pool directory {key} ignored ({reason}); "
          f"the packaged pool answers instead", file=sys.stderr)


def read_user_manifest(name: str, *, warn: bool) -> PoolManifest | None:
    """The user's manifest for ``name``, or None when there is none.

    A user directory whose manifest is missing or invalid warns (when
    ``warn``) and answers None: the caller falls back to the packaged
    pool. A missing user directory is silence, not a warning.
    """
    directory = user_dir(name)
    if not directory.is_dir():
        return None
    try:
        return load_manifest(directory)
    except InvalidManifest as exc:
        if warn:
            warn_once(directory, exc)
        return None


def valid_user_names() -> list[str]:
    """User pool names whose manifests validate (quietly: no warnings)."""
    return [name for name in user_names()
            if read_user_manifest(name, warn=False) is not None]


def describe(name: str) -> tuple[PoolManifest | None, str]:
    """A pool's manifest and where it came from.

    Returns ``(manifest, source)`` with source ``"user"`` for a valid
    user manifest, else the packaged manifest with source
    ``"packaged"``, else ``(None, "packaged")`` for a registered adapter
    with no directory at all. A broken user copy warns once and falls
    back to the packaged one.
    """
    manifest = read_user_manifest(name, warn=True)
    if manifest is not None:
        return manifest, "user"
    try:
        return load_manifest(packaged_dir(name)), "packaged"
    except InvalidManifest:
        return None, "packaged"


def _mapping(manifest: PoolManifest, ctx: LaunchContext) -> dict[str, str]:
    return {
        "session_id": ctx.session.id or "",
        "model": manifest.model,
        "effort": ctx.effort,
        "worktree": str(ctx.worktree),
        "job_path": str(ctx.job_path),
        "role_path": str(ctx.role_path),
        "log_path": str(ctx.log_path),
        "pid_path": str(ctx.pid_path),
        "verdict_path": str(ctx.verdict_path),
        "scratch_dir": str(ctx.scratch_dir),
        "branch": ctx.branch,
        "target": ctx.target,
        "timeout": ctx.timeout,
    }


def substitute(text: str, mapping: dict[str, str]) -> str:
    """Replace ``{names}`` the mapping knows; leave other braces alone."""
    for key, value in mapping.items():
        text = text.replace("{" + key + "}", value)
    return text


def vendor_command(manifest: PoolManifest,
                   ctx: LaunchContext) -> list[str]:
    """The vendor argv a directory-declared pool runs."""
    assert manifest.vendor_argv is not None
    mapping = _mapping(manifest, ctx)
    return [substitute(part, mapping) for part in manifest.vendor_argv]


def vendor_inner(manifest: PoolManifest, ctx: LaunchContext) -> str:
    """The worker shell for a directory-declared pool."""
    stdin: Path | None = None
    if manifest.vendor_stdin == "job":
        stdin = ctx.job_path
    return _common.wrap_inner(
        vendor_command(manifest, ctx),
        pid_path=ctx.pid_path,
        log_path=ctx.log_path,
        stdin_path=stdin,
    )


def vendor_outer(manifest: PoolManifest, ctx: LaunchContext) -> list[str]:
    """The detached spawn for a directory-declared pool."""
    return _common.wrap_outer(ctx.session.id, vendor_inner(manifest, ctx),
                              window=ctx.window,
                              script_path=ctx.pid_path.parent / "run.sh",
                              worktree=ctx.worktree,
                              timeout=ctx.timeout)


def _vendor_binary(manifest: PoolManifest) -> str:
    """The executable Capacity counts for a directory pool.

    A ``[vendor]`` argv names it in argv[0]. Otherwise the packaged
    adapter this directory wraps names it. A pool that names neither
    counts nothing.
    """
    if manifest.vendor_argv:
        from ..procs import executable_of

        return executable_of(manifest.vendor_argv[0])
    if manifest.adapter:
        from . import REGISTRY

        impl = REGISTRY.get(manifest.adapter)
        value = getattr(impl, "binary", "") if impl is not None else ""
        if isinstance(value, str) and value:
            return value
    return ""


def _binary_is_foreman_worker(manifest: PoolManifest) -> bool:
    """Whether this directory pool's vendor binary is only ever a Foreman worker.

    A ``[vendor]`` argv is the user's own command, so the default
    stands. Otherwise the packaged adapter this directory wraps
    declares it, the way ``binary`` is read. A pool that names
    neither defaults to True.
    """
    if manifest.vendor_argv:
        return True
    if manifest.adapter:
        from . import REGISTRY

        impl = REGISTRY.get(manifest.adapter)
        if impl is not None:
            return bool(getattr(impl, "binary_is_foreman_worker", True))
    return True


class DirectoryPool(PoolAdapter):
    """A pool read from a directory: a user override or a new pool.

    Identity (name, model, timeout, interactive, roles) always comes
    from the manifest. Behaviour comes from the manifest's ``[vendor]``
    command when it declares one, else from the named packaged adapter,
    so a clone edited only in prose launches exactly what it always did.
    """

    def __init__(self, manifest: PoolManifest) -> None:
        self.manifest = manifest
        self.name = manifest.name
        self.model = manifest.model
        self.timeout_default = manifest.timeout_default
        self.interactive = manifest.interactive
        self.roles: tuple[str, ...] = manifest.roles
        self.binary = _vendor_binary(manifest)
        self.binary_is_foreman_worker = _binary_is_foreman_worker(manifest)

    def _impl(self) -> PoolAdapter | None:
        if self.manifest.adapter is None:
            return None
        from . import REGISTRY

        return REGISTRY.get(self.manifest.adapter)

    def command_str(self, ctx: LaunchContext) -> str:
        impl = self._impl()
        if self.manifest.vendor_argv is not None:
            return _common.printable_command(
                vendor_outer(self.manifest, ctx),
                vendor_inner(self.manifest, ctx))
        if impl is not None:
            return impl.command_str(ctx)
        return (f"(pool {self.name!r} names neither a vendor command "
                f"nor a registered adapter; edit its manifest to add one)")

    def launch(self, ctx: LaunchContext) -> int:
        impl = self._impl()
        if self.manifest.vendor_argv is None:
            if impl is not None:
                return impl.launch(ctx)
            raise RuntimeError(
                f"pool {self.name!r} names neither a vendor command nor "
                f"a registered adapter; edit its manifest to add one")
        _common.write_worker_script(ctx.pid_path.parent / "run.sh",
                                    vendor_inner(self.manifest, ctx))
        return _common.spawn_and_wait(
            vendor_outer(self.manifest, ctx), pid_path=ctx.pid_path,
            session_id=ctx.session.id, popen=Popen,
        )

    def observe(self, session: Session) -> dict:
        impl = self._impl()
        if impl is not None and self.manifest.vendor_argv is None:
            return impl.observe(session)
        return _common.observe_session(session)

    def usage(self, session: Session) -> dict | None:
        impl = self._impl()
        if impl is not None and self.manifest.vendor_argv is None:
            return impl.usage(session)
        return None

    def refusal(self, session: Session) -> dict | None:
        impl = self._impl()
        if impl is not None and self.manifest.vendor_argv is None:
            return impl.refusal(session)
        return None

    def verdict(self, path: Path | str) -> dict:
        """A review job's verdict file, normalised to pass/fail + summary."""
        impl = self._impl()
        if impl is not None and self.manifest.vendor_argv is None:
            verdict = getattr(impl, "verdict", None)
            if callable(verdict):
                return verdict(path)
        return _common.read_verdict(path)

    def meter(self, session: Session) -> dict | None:
        """No pool reports a quota meter yet: no number, never invented."""
        return None
