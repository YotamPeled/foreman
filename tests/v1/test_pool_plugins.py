"""Pools as plugin directories: layout, overrides, verbs, fifth pool.

No test here runs a vendor CLI or opens a window. The fake vendor is a
bare ``sh`` command declared in the fixture pool's manifest; where a
launch must really start, a Popen double runs the worker script the
launcher hands over (the seam below systemd-run) instead of spawning a
unit. Expected manifests are hand-derived literals, never built with
the loader's own helpers.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman import cli, paths, store
from foreman.collector import tick
from foreman.entities import Session
from foreman.pool import (
    pool_add_main,
    pool_clone_main,
    pool_list_main,
    pool_remove_main,
)
from foreman.pools import LaunchContext
from foreman.pools import get as get_pool
from foreman.pools import names as pool_names
from foreman.pools import plugins
from foreman.pools.muse import MuseAdapter

FIXTURE_POOL = Path(__file__).with_name("fixture-pools") / "fakepool"

SPEC_OK = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v1 -q\n"
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    return tmp_path


def write_user_pool(root: Path, name: str, manifest: str) -> Path:
    dest = Path(paths.config_dir()) / "pools" / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.toml").write_text(manifest, encoding="utf-8")
    return dest


def make_ctx(tmp: Path, pool: str, model: str,
             sid: str = "ses-plug01") -> LaunchContext:
    session = Session(id=sid, role="muse", pool=pool, model=model,
                      state="running")
    return LaunchContext(
        session=session,
        worktree=tmp / "wt",
        job_path=tmp / "wt" / "FOREMAN-JOB.md",
        role_path=tmp / "wt" / "FOREMAN-ROLE.md",
        log_path=tmp / "sessions" / sid / "log",
        pid_path=tmp / "sessions" / sid / "pid",
        verdict_path=tmp / "sessions" / sid / "verdict.json",
        scratch_dir=tmp / "sessions" / sid / "scratch",
        branch=f"foreman/{sid}",
        target="main",
        timeout="20m",
        effort="high",
        kind="implement",
    )


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=path, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "test")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("add", "seed.txt")
    run("commit", "-qm", "seed")
    return path


def session_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("session: "):
            return line.split("session: ", 1)[1].strip()
    raise AssertionError(f"no session line in output:\n{out}")


@pytest.mark.parametrize("name,model,timeout,roles,adapter", [
    ("muse", "muse-spark-1.3-contributor", "20m", ["muse"], "muse"),
    ("grok", "grok-4.6", "20m", ["grok"], "grok"),
    ("claude", "claude-opus-5", "20m",
     ["opus", "supervisor", "foreman"], "claude"),
    ("codex", "gpt-6-astra", "25m", ["astra"], "codex"),
])
def test_packaged_manifest_matches_its_adapter(env, name, model, timeout,
                                               roles, adapter):
    """The directory and the implementation must agree: a manifest whose
    model or timeout drifted from its adapter would list one pool and
    launch another."""
    from foreman import config as config_module

    manifest = plugins.load_manifest(plugins.packaged_dir(name))
    assert manifest.name == name
    assert manifest.model == model == get_pool(name).model
    assert manifest.timeout_default == timeout == \
        get_pool(name).timeout_default
    assert manifest.interactive is False
    assert manifest.adapter == adapter
    assert list(manifest.roles) == roles
    shipped = config_module.defaults()
    assert list(manifest.roles) == list(shipped[name]["roles"]), name
    skill = plugins.packaged_dir(name) / plugins.SKILL_FILENAME
    assert skill.is_file() and skill.stat().st_size > 0


def test_pool_list_names_the_four_packaged_pools(env, capsys):
    """`foreman pool list` is the deliverable's smoke signal: the four
    packaged pools, each named with its model and source."""
    assert pool_list_main() == 0
    out = capsys.readouterr().out
    for name in ("muse", "grok", "claude", "codex"):
        assert name in out
    assert "[packaged]" in out
    assert "muse-spark-1.3-contributor" in out


def test_user_override_replaces_packaged_entirely(env, capsys):
    """A user copy wins wholesale: identity comes from the manifest, no
    field is merged with the packaged one, and behaviour still
    delegates to the named adapter."""
    write_user_pool(env, "muse",
                    'name = "muse"\n'
                    'model = "custom-model-1"\n'
                    'timeout_default = "7m"\n'
                    'interactive = false\n'
                    'roles = ["muse", "opus"]\n'
                    'adapter = "muse"\n')
    adapter = get_pool("muse")
    assert isinstance(adapter, plugins.DirectoryPool)
    assert adapter.model == "custom-model-1"
    assert adapter.timeout_default == "7m"
    assert adapter.roles == ("muse", "opus")
    ctx = make_ctx(env, "muse", MuseAdapter.model)
    assert adapter.command_str(ctx) == MuseAdapter().command_str(ctx)
    assert capsys.readouterr().err == ""


BROKEN_MUSE_MANIFESTS = [
    "not = toml [[\n",
    'name = "grok"\nmodel = "x"\ntimeout_default = "5m"\nadapter = "muse"\n',
    'name = "muse"\nmodel = "x"\ntimeout_default = "soon"\nadapter = "muse"\n',
    'name = "muse"\nmodel = "x"\ntimeout_default = "5m"\nadapter = "nope"\n',
    'name = "muse"\nmodel = ""\ntimeout_default = "5m"\nadapter = "muse"\n',
]


@pytest.mark.parametrize("body", BROKEN_MUSE_MANIFESTS)
def test_broken_user_copy_falls_back_with_one_warning(env, capsys, body):
    """Three-deep ladder, third rung: a user copy whose manifest does not
    validate is ignored with one warning naming the directory, and the
    packaged pool answers — never a crash, never a second warning."""
    dest = write_user_pool(env, "muse", body)
    first = get_pool("muse")
    assert isinstance(first, MuseAdapter)
    err = capsys.readouterr().err
    assert str(dest) in err
    assert "warning" in err
    second = get_pool("muse")
    assert isinstance(second, MuseAdapter)
    assert capsys.readouterr().err == ""


def test_user_directory_without_a_manifest_falls_back(env, capsys):
    """An empty user directory is a broken copy too: warn naming it,
    answer with the packaged pool."""
    dest = Path(paths.config_dir()) / "pools" / "muse"
    dest.mkdir(parents=True, exist_ok=True)
    assert isinstance(get_pool("muse"), MuseAdapter)
    assert str(dest) in capsys.readouterr().err


def test_clone_copies_packaged_for_editing(env, capsys):
    """Cloning is how a packaged pool gets edited: the copy is byte for
    byte the packaged directory, still launches what it always did, and
    a second clone is refused instead of silently overwriting edits."""
    assert pool_clone_main("muse") == 0
    out = capsys.readouterr().out
    dest = Path(paths.config_dir()) / "pools" / "muse"
    assert "cloned" in out and str(dest) in out
    for filename in ("manifest.toml", "SKILL.md"):
        assert (dest / filename).read_bytes() == \
            (plugins.packaged_dir("muse") / filename).read_bytes()
    ctx = make_ctx(env, "muse", MuseAdapter.model)
    clone = get_pool("muse")
    assert isinstance(clone, plugins.DirectoryPool)
    assert clone.command_str(ctx) == MuseAdapter().command_str(ctx)
    assert pool_clone_main("muse") == 1
    assert "already exists" in capsys.readouterr().err
    assert pool_clone_main("no-such-pool") == 1
    assert "unknown packaged pool" in capsys.readouterr().err


def test_remove_falls_back_to_packaged(env, capsys):
    """Removing a clone leaves no hole: the packaged pool answers again.
    Removing what was never cloned is refused, not silently accepted."""
    assert pool_clone_main("muse") == 0
    capsys.readouterr()
    assert pool_remove_main("muse") == 0
    out = capsys.readouterr().out
    assert "packaged 'muse' answers again" in out
    assert not (Path(paths.config_dir()) / "pools" / "muse").exists()
    assert isinstance(get_pool("muse"), MuseAdapter)
    assert pool_remove_main("muse") == 1
    assert "nothing to remove" in capsys.readouterr().err


def test_remove_of_a_brand_new_pool_leaves_no_hole_behind(env, capsys):
    """A user pool with no packaged counterpart removes cleanly: the
    name stops resolving instead of pointing at a deleted directory."""
    assert pool_add_main("fakepool", from_dir=str(FIXTURE_POOL)) == 0
    capsys.readouterr()
    assert pool_remove_main("fakepool") == 0
    assert "removed user pool 'fakepool'" in capsys.readouterr().out
    assert "fakepool" not in pool_names()
    with pytest.raises(ValueError, match="unknown pool"):
        get_pool("fakepool")


def test_add_scaffolds_a_listable_pool_that_refuses_to_launch(env, capsys):
    """`pool add` without `--from` scaffolds: the manifest validates and
    lists, and a launch of the implementation-less pool is refused with
    the reason — never a crash, never a silent no-op."""
    assert pool_add_main("newpool") == 0
    dest = Path(paths.config_dir()) / "pools" / "newpool"
    assert "added" in capsys.readouterr().out
    manifest = plugins.load_manifest(dest)
    assert manifest.name == "newpool"
    assert "newpool" in pool_names()
    assert (dest / plugins.SKILL_FILENAME).is_file()
    adapter = get_pool("newpool")
    assert isinstance(adapter, plugins.DirectoryPool)
    ctx = make_ctx(env, "newpool", manifest.model)
    with pytest.raises(RuntimeError, match="neither a vendor"):
        adapter.launch(ctx)
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    rc = cli.main(["launch", "muse", "newpool", str(spec),
                   "--repo", str(repo),
                   "--worktree", str(env / "wt-newpool")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "failed to start the process" in err
    assert "newpool" in err
    assert pool_add_main("newpool") == 1
    assert "already exists" in capsys.readouterr().err
    assert pool_add_main("has space") == 1
    assert "must look like a pool name" in capsys.readouterr().err


def test_add_from_installs_a_pool_directory(env, capsys):
    """`pool add --from` installs a directory as a pool. Refused: a
    missing directory, one holding no pool, and one holding a
    differently-named pool (which would otherwise shadow under the
    wrong name)."""
    assert pool_add_main("fakepool", from_dir=str(FIXTURE_POOL)) == 0
    assert "fakepool" in pool_names()
    installed = plugins.load_manifest(
        Path(paths.config_dir()) / "pools" / "fakepool")
    assert installed.name == "fakepool"
    assert installed.vendor_argv == ("sh", "-c", "echo fake-vendor-output")
    assert pool_add_main("other", from_dir=str(FIXTURE_POOL)) == 1
    assert "not 'other'" in capsys.readouterr().err
    assert pool_add_main("empty", from_dir=str(env / "no-such-dir")) == 1
    assert "not a directory" in capsys.readouterr().err
    bare = env / "bare"
    bare.mkdir()
    assert pool_add_main("bare", from_dir=str(bare)) == 1
    assert "holds no pool" in capsys.readouterr().err


class DirectPopen:
    """Stands in for ``subprocess.Popen`` below systemd-run: finds the
    worker script the launch hands over and runs it for real with bash,
    so the manifest's vendor command, the pid file and the finish
    marker are all exercised. Only the unit spawn itself is replaced."""

    def __init__(self, argv, **kwargs):
        self.pid = 1
        script = next(str(part) for part in argv if str(part).endswith("run.sh"))
        subprocess.run(["bash", script], check=False,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True)


def test_fifth_pool_launches_a_job_end_to_end(env, monkeypatch, capsys):
    """A pool added as a directory carries a job end to end: `pool add`
    installs the fixture, `launch` starts its fake vendor through the
    real wrapper, the log carries the vendor output plus a finish
    marker of its own line, the pool observes completion, and the
    collector's tick returns the job."""
    from foreman.pools import plugins as plugins_module

    assert pool_add_main("fakepool", from_dir=str(FIXTURE_POOL)) == 0
    capsys.readouterr()
    assert pool_list_main() == 0
    assert "fakepool fake-test-model [user]" in capsys.readouterr().out

    monkeypatch.setattr(plugins_module, "Popen", DirectPopen)
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    worktree = str(env / "wt-fake")
    rc = cli.main(["launch", "muse", "fakepool", str(spec),
                   "--repo", str(repo), "--worktree", worktree,
                   "--front", "comp", "--job", "job-e2e"])
    assert rc == 0
    out = capsys.readouterr().out
    sid = session_line(out)
    log_path = next(line for line in out.splitlines()
                    if line.startswith("log: ")).split("log: ", 1)[1].strip()
    lines = Path(log_path).read_text(encoding="utf-8").splitlines()
    assert "fake-vendor-output" in lines
    assert "### finished rc=0" in lines

    roster = store.read_snapshot(paths.roster_path(), default={})
    record = roster["sessions"][sid]
    assert record["pool"] == "fakepool"
    assert record["model"] == "fake-test-model"
    assert record["state"] == "running"
    seen = get_pool("fakepool").observe(Session.from_dict(record))
    assert seen["finish_present"] is True
    assert seen["finish_rc"] == 0

    tick(now=datetime.now(timezone.utc))
    jobs = {line["id"]: line["state"]
            for line in store.read_ledger(paths.front_jobs_path("comp"))}
    assert jobs["job-e2e"] == "returned"


def test_user_claude_pool_launches_its_manifest_model(env, capsys):
    """A user pool wrapping the claude adapter launches the model its
    manifest named, not the packaged role pin. The packaged claude pool
    still launches the role's model. No ``[vendor]`` table: the adapter
    is the implementation, and the session model is what it is started
    with."""
    write_user_pool(env, "fable",
                    'name = "fable"\n'
                    'model = "claude-fable-5-1"\n'
                    'timeout_default = "20m"\n'
                    'interactive = false\n'
                    'roles = ["opus"]\n'
                    'adapter = "claude"\n')
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    rc = cli.main(["launch", "opus", "fable", str(spec),
                   "--repo", str(repo),
                   "--worktree", str(env / "wt-fable"),
                   "--dry-run"])
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert "--model claude-fable-5-1" in out
    assert "--model claude-opus-5" not in out

    rc = cli.main(["launch", "opus", "claude", str(spec),
                   "--repo", str(repo),
                   "--worktree", str(env / "wt-claude"),
                   "--dry-run"])
    assert rc == 0, capsys.readouterr().err
    packaged = capsys.readouterr().out
    assert "--model claude-opus-5" in packaged
    assert "--model claude-fable-5-1" not in packaged


def test_directory_pool_without_adapter_reports_no_meter(env):
    """A directory-declared pool has no token counter and no quota
    meter: usage and meter answer nothing, while a review verdict still
    reads through the one shared reading every pool uses."""
    assert pool_add_main("fakepool", from_dir=str(FIXTURE_POOL)) == 0
    adapter = get_pool("fakepool")
    session = Session(id="ses-meter1", role="muse", pool="fakepool",
                      model="fake-test-model", pid=None, state="running")
    assert adapter.usage(session) is None
    assert adapter.meter(session) is None
    verdict = env / "verdict.json"
    verdict.write_text('{"verdict": "pass", "summary": "clean read"}',
                       encoding="utf-8")
    assert adapter.verdict(verdict) == {"passed": True,
                                        "summary": "clean read"}
    with pytest.raises(ValueError):
        adapter.verdict(env / "no-such-verdict.json")


def test_wrapping_claude_without_vendor_is_not_a_foreman_worker(env):
    """A user pool wrapping the claude adapter, with no [vendor] argv,
    inherits the packaged exemption: the executable is the owner's."""
    write_user_pool(env, "fable",
                    'name = "fable"\n'
                    'model = "claude-opus-5"\n'
                    'timeout_default = "20m"\n'
                    'interactive = false\n'
                    'adapter = "claude"\n')
    adapter = get_pool("fable")
    assert adapter.binary_is_foreman_worker is False
    assert plugins._binary_is_foreman_worker(adapter.manifest) is False


def test_vendor_argv_named_claude_is_not_a_foreman_worker(env):
    """A [vendor] argv whose basename is claude is the owner's binary,
    even when the pool is not named claude."""
    write_user_pool(env, "fable",
                    'name = "fable"\n'
                    'model = "claude-opus-5"\n'
                    'timeout_default = "20m"\n'
                    'interactive = false\n'
                    'adapter = "claude"\n'
                    '[vendor]\n'
                    'argv = ["claude", "-p"]\n')
    adapter = get_pool("fable")
    assert adapter.binary == "claude"
    assert adapter.binary_is_foreman_worker is False


def test_vendor_argv_symlink_to_claude_is_not_a_foreman_worker(env):
    """A [vendor] argv followed through a symlink to a file named
    claude is the owner's binary. The wrapper's own name is not."""
    bindir = env / "bin"
    bindir.mkdir()
    target = bindir / "claude"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(target, 0o755)
    link = bindir / "claude-fable"
    link.symlink_to(target)
    write_user_pool(env, "fable",
                    'name = "fable"\n'
                    'model = "claude-opus-5"\n'
                    'timeout_default = "20m"\n'
                    'interactive = false\n'
                    '[vendor]\n'
                    f'argv = ["{link}", "-p"]\n')
    adapter = get_pool("fable")
    assert adapter.binary == "claude-fable"
    assert adapter.binary_is_foreman_worker is False


def test_vendor_argv_bare_name_resolving_to_claude_is_not_a_foreman_worker(
        env, monkeypatch):
    """A bare [vendor] argv[0] that shutil.which finds as a symlink
    to claude is the owner's binary too."""
    bindir = env / "bin"
    bindir.mkdir()
    target = bindir / "claude"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(target, 0o755)
    link = bindir / "claude-fable"
    link.symlink_to(target)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    write_user_pool(env, "fable",
                    'name = "fable"\n'
                    'model = "claude-opus-5"\n'
                    'timeout_default = "20m"\n'
                    'interactive = false\n'
                    '[vendor]\n'
                    'argv = ["claude-fable", "-p"]\n')
    adapter = get_pool("fable")
    assert adapter.binary == "claude-fable"
    assert adapter.binary_is_foreman_worker is False


def test_vendor_argv_grokish_is_a_foreman_worker(env):
    """A [vendor] argv that resolves to nothing named claude stays
    Foreman-owned: the default must not flip for an unknown binary."""
    write_user_pool(env, "fable",
                    'name = "fable"\n'
                    'model = "claude-opus-5"\n'
                    'timeout_default = "20m"\n'
                    'interactive = false\n'
                    '[vendor]\n'
                    'argv = ["grokish", "--prompt-file", "job.md"]\n')
    adapter = get_pool("fable")
    assert adapter.binary == "grokish"
    assert adapter.binary_is_foreman_worker is True
