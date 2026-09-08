"""Launcher and Muse adapter: fake-pool launches, refusals, dry-run."""

import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

from foreman import cli, paths, store
from foreman import launch as launch_module
from foreman.entities import Session
from foreman.pools import LaunchContext, PoolAdapter, get as get_pool
from foreman.pools import muse as muse_pool
from foreman.pools.muse import MuseAdapter

RULING_A = "Verify by re-running; a worker's word is PLAUSIBLE until then."
RULING_B = "A question a ruling already covers is never raised again."
SPEC_OK = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v0 -q\n"
    "Do not touch src/foreman/store.py.\n"
)


class FakeAdapter(PoolAdapter):
    """Fake pool: writes the pid file and returns, spawning nothing real."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False

    def launch(self, ctx: LaunchContext) -> int:
        pid = os.getpid()
        ctx.pid_path.write_text(f"{pid}\n", encoding="utf-8")
        return pid

    def observe(self, session: Session) -> dict:
        return {"transcript_mtime": None, "cpu_s": 0.0, "finish_present": False}

    def command_str(self, ctx: LaunchContext) -> str:
        return f"fake-exec --worktree {ctx.worktree} --prompt-file {ctx.job_path}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    return tmp_path


@pytest.fixture()
def fake_pool():
    from foreman import pools

    pools.register("fake", FakeAdapter())
    try:
        yield FakeAdapter()
    finally:
        pools.unregister("fake")


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


def write_spec(root: Path, name: str, text: str) -> str:
    spec = root / name
    spec.write_text(text, encoding="utf-8")
    return str(spec)


def seed_rulings(*texts: str) -> None:
    for text in texts:
        store.append_ledger(
            paths.rulings_path(),
            {"scope": "swarm", "text": text, "source": "owner"},
        )


def launch(argv: list[str]) -> int:
    return cli.main(["launch", *argv])


def session_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("session: "):
            return line.split("session: ", 1)[1].strip()
    raise AssertionError(f"no session line in output:\n{out}")


def test_launch_records_session_worktree_log_and_roster(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    seed_rulings(RULING_A, RULING_B)
    worktree = str(env / "wt-one")

    rc = launch(["muse", "fake", spec, "--repo", str(repo),
                 "--worktree", worktree, "--front", "corpus",
                 "--task", "second reads", "--job", "job-1",
                 "--units", "2-4", "--scope", "WHAT: x\nINPUTS: y\n"])
    assert rc == 0
    out = capsys.readouterr().out
    sid = session_line(out)
    assert sid.startswith("ses-")
    assert f"worktree: {worktree}" in out
    assert f"pid: {os.getpid()}" in out

    log = next(line for line in out.splitlines() if line.startswith("log: "))
    log_path = log.split("log: ", 1)[1].strip()
    assert log_path == str(env / "state" / "sessions" / sid / "log")
    assert Path(log_path).is_file()
    assert (env / "state" / "sessions" / sid / "pid").read_text(
        encoding="utf-8").strip() == str(os.getpid())

    job_file = Path(worktree) / "FOREMAN-JOB.md"
    role_file = Path(worktree) / "FOREMAN-ROLE.md"
    job_text = job_file.read_text(encoding="utf-8")
    role_text = role_file.read_text(encoding="utf-8")
    assert '# Job job-1 \u00b7 implement \u00b7 task "second reads" \u00b7 front corpus' in job_text
    assert "units: 2-4" in job_text
    assert SPEC_OK in job_text
    assert "WHAT: x\nINPUTS: y\n" in job_text
    for ruling in (RULING_A, RULING_B):
        assert ruling in job_text
        assert ruling in role_text
    assert "no Foreman tools" in role_text
    assert "no Foreman verb" in role_text
    assert "no ledger" in role_text
    assert "{{" not in role_text and "}}" not in role_text
    for key in ("- worktree: ", "- branch: ", "- target: ",
                "- scratch directory: ", "- finish marker path: ",
                "- verdict path: ", "- timeout: 5m"):
        assert key in job_text
    for line in job_text.splitlines():
        if line.startswith(("- worktree: ", "- scratch directory: ",
                             "- finish marker path: ", "- verdict path: ")):
            assert os.path.isabs(line.split(": ", 1)[1].split(" ")[0]), line
    assert f"- worktree: {worktree}" in job_text

    roster = json.loads((env / "state" / "roster.json").read_text(encoding="utf-8"))
    assert set(roster) == {"sessions"}
    assert set(roster["sessions"]) == {sid}
    record = roster["sessions"][sid]
    assert set(record) == {f.name for f in dataclasses.fields(Session)}
    assert record["role"] == "muse"
    assert record["pool"] == "fake"
    assert record["model"] == "fake-test-model"
    assert record["front"] == "corpus"
    assert record["job"] == "job-1"
    assert record["state"] == "running"
    assert isinstance(record["pid"], int)


def test_second_launch_never_reuses_log(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) == 0
    first_log = next(
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("log: ")
    ).split("log: ", 1)[1]
    Path(first_log).write_text("first stay\n", encoding="utf-8")

    assert launch(["muse", "fake", spec, "--repo", str(repo)]) == 0
    second_log = next(
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("log: ")
    ).split("log: ", 1)[1]
    assert second_log != first_log
    assert Path(first_log).read_text(encoding="utf-8") == "first stay\n"
    assert Path(second_log).is_file()


def test_overlong_spec_refused(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    long_spec = "".join(f"filler line {n}\n" for n in range(121)) + "verify: pytest x\n"
    spec = write_spec(env, "long.md", long_spec)
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) != 0
    err = capsys.readouterr().err
    assert "one page" in err
    assert "120" in err
    assert "verification" not in err
    assert not paths.roster_path().exists()


def test_spec_without_verification_refused(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "nvc.md", "Do the thing.\nIn the worktree.\n")
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) != 0
    err = capsys.readouterr().err
    assert "verification" in err
    assert "one page" not in err
    assert not paths.roster_path().exists()


def test_both_spec_reasons_named_at_once(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    both = "".join(f"filler line {n}\n" for n in range(121))
    spec = write_spec(env, "both.md", both)
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) != 0
    err = capsys.readouterr().err
    assert "one page" in err
    assert "verification" in err


def test_exactly_120_lines_with_verify_passes(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    text = "".join(f"filler line {n}\n" for n in range(119)) + "verify: pytest x\n"
    assert len(text.splitlines()) == 120
    spec = write_spec(env, "edge.md", text)
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) == 0
    capsys.readouterr()


@pytest.mark.parametrize("flag,spec_rel", [
    ("spec", "rel/spec.md"),
    ("worktree", None),
    ("log", None),
])
def test_relative_paths_refused(env, fake_pool, capsys, flag, spec_rel):
    repo = make_repo(env / "repo")
    good_spec = write_spec(env, "ok.md", SPEC_OK)
    argv = ["muse", "fake", spec_rel or good_spec, "--repo", str(repo)]
    if flag == "worktree":
        argv += ["--worktree", "rel/wt"]
    if flag == "log":
        argv += ["--log", "rel/log"]
    assert launch(argv) != 0
    err = capsys.readouterr().err
    assert f"relative {flag} path" in err


def test_timeout_default_and_override(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) == 0
    first = session_line(capsys.readouterr().out)
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--timeout", "42m"]) == 0
    second = session_line(capsys.readouterr().out)
    assert first != second
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert set(roster["sessions"]) == {first, second}


def test_timeout_visible_in_job_file(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--worktree", str(env / "wt-a")]) == 0
    capsys.readouterr()
    default_text = (env / "wt-a" / "FOREMAN-JOB.md").read_text(encoding="utf-8")
    assert "- timeout: 5m" in default_text
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--worktree", str(env / "wt-b"),
                   "--timeout", "42m"]) == 0
    capsys.readouterr()
    assert "- timeout: 42m" in (env / "wt-b" / "FOREMAN-JOB.md").read_text(
        encoding="utf-8")
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--timeout", "soon"]) != 0
    assert "bad timeout" in capsys.readouterr().err


def test_unknown_pool_and_role_refused(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    assert launch(["muse", "nope", spec, "--repo", str(repo)]) != 0
    assert "unknown pool" in capsys.readouterr().err
    assert launch(["nope", "fake", spec, "--repo", str(repo)]) != 0
    assert "unknown role" in capsys.readouterr().err


def test_log_file_never_reused(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    taken = env / "taken.log"
    taken.write_text("previous run\n", encoding="utf-8")
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--log", str(taken)]) != 0
    assert "never reused" in capsys.readouterr().err
    assert taken.read_text(encoding="utf-8") == "previous run\n"


def test_bad_units_refused(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--units", "banana"]) != 0
    assert "bad units" in capsys.readouterr().err


@pytest.mark.parametrize("effort", ["high", "xhigh"])
def test_dry_run_prints_muse_command(env, capsys, effort):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    worktree = str(env / "wt-dry")
    rc = launch(["muse", "muse", spec, "--repo", str(repo),
                 "--worktree", worktree, "--effort", effort, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    sid = session_line(out)
    assert "muse exec" in out
    assert "--model muse-spark-1.3-contributor" in out
    assert f"--reasoning-effort {effort}" in out
    assert "--approval-mode never" in out
    assert "--json" in out
    assert "--yolo" not in out
    assert f"--workspace {worktree}" in out
    assert "--prompt-file" in out and "FOREMAN-JOB.md" in out
    assert "### finished rc=$?" in out
    assert out.index("### finished rc=$?") < out.index("| tee")
    assert "(not started --dry-run)" in out
    # A dry run starts nothing, so it records nothing: the files exist,
    # but the roster is left as found and no pid file is written.
    assert (Path(worktree) / "FOREMAN-JOB.md").is_file()
    assert (Path(worktree) / "FOREMAN-ROLE.md").is_file()
    assert not paths.session_pid_path(sid).exists()
    assert not paths.roster_path().exists()


def snapshot(root: Path) -> dict[Path, tuple[int, int]]:
    return {p: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def test_launch_writes_only_state_worktree_and_git(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    seed_rulings(RULING_A)
    before = snapshot(env)
    worktree = env / "wt-contained"
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--worktree", str(worktree)]) == 0
    out = capsys.readouterr().out
    sid = session_line(out)
    state = paths.state_dir()
    git_dir = repo / ".git"
    after = snapshot(env)
    offenders = []
    for path, stamp in after.items():
        if before.get(path) == stamp:
            continue
        if path.is_relative_to(state):
            continue
        if path.is_relative_to(worktree):
            continue
        if path.is_relative_to(git_dir):
            continue
        offenders.append(str(path))
    assert offenders == []
    assert not (env / "home").exists()
    # The state directory is watched file by file too: only the roster,
    # the lock and the new session's own files may change under it.
    session_files = state / "sessions" / sid
    strays = sorted(
        str(path) for path, stamp in after.items()
        if before.get(path) != stamp
        and path.is_relative_to(state)
        and session_files not in path.parents
        and not (path.parent == state
                 and path.name in ("roster.json", "lock"))
    )
    assert strays == []


def make_ctx(tmp: Path, sid: str = "ses-test123",
             window: bool = False) -> LaunchContext:
    session = Session(id=sid, role="muse", pool="muse",
                      model=MuseAdapter.model, state="running")
    return LaunchContext(
        session=session,
        worktree=tmp / "wt",
        job_path=tmp / "wt" / "FOREMAN-JOB.md",
        role_path=tmp / "wt" / "FOREMAN-ROLE.md",
        log_path=tmp / "sessions" / sid / "log",
        pid_path=tmp / "sessions" / sid / "pid",
        verdict_path=tmp / "sessions" / sid / "verdict.json",
        scratch_dir=tmp / "sessions" / sid / "scratch",
        branch="foreman/ses-test123",
        target="main",
        timeout="20m",
        effort="high",
        window=window,
    )


def test_muse_argv_is_exactly_the_proven_shape(env):
    ctx = make_ctx(env)
    assert muse_pool.muse_argv(ctx) == [
        "muse", "exec",
        "--model", "muse-spark-1.3-contributor",
        "--reasoning-effort", "high",
        "--approval-mode", "never",
        "--json",
        "--workspace", str(ctx.worktree),
        "--prompt-file", str(ctx.job_path),
    ]


def test_muse_marker_reaches_the_log_file(env, monkeypatch):
    ctx = make_ctx(env)
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    bindir = env / "bin"
    bindir.mkdir()
    stub = bindir / "muse"
    stub.write_text("#!/bin/sh\necho vendor-output\nexit 3\n", encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    inner = muse_pool.inner_command(ctx)
    assert "### finished rc=$?" in inner
    assert inner.index("### finished rc=$?") < inner.index("| tee")
    assert str(ctx.pid_path) in inner
    assert str(ctx.log_path) in inner
    # Textual ordering alone would pass for a marker sent to an unpiped
    # stream, so run the real command and read the log file back.
    subprocess.run(["bash", "-c", inner], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = ctx.log_path.read_text(encoding="utf-8").splitlines()
    assert "vendor-output" in lines
    assert "### finished rc=3" in lines


def test_muse_detach_uses_the_window_launcher_only_when_asked(env, monkeypatch):
    ctx = make_ctx(env, window=True)
    monkeypatch.setenv("FOREMAN_WINDOW_LAUNCHER", "test-launcher")
    argv = muse_pool.outer_argv(ctx)
    assert argv == ["test-launcher", f"foreman-{ctx.session.id}", "bash",
                    str(ctx.pid_path.parent / "run.sh")]
    assert "muse exec" in muse_pool.inner_command(ctx)


def test_a_configured_launcher_alone_opens_no_window(env, monkeypatch):
    """Swarm sessions take no desktop workspace unless a launch asks for one."""
    ctx = make_ctx(env)
    monkeypatch.setenv("FOREMAN_WINDOW_LAUNCHER", "test-launcher")
    assert muse_pool.outer_argv(ctx)[0] == "systemd-run"


def test_muse_detach_defaults_to_systemd_run(env, monkeypatch):
    ctx = make_ctx(env)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    argv = muse_pool.outer_argv(ctx)
    # The worker is handed over as a script file, never as text on a
    # systemd command line, where $$ means a literal dollar.
    assert argv == ["systemd-run", "--user",
                    f"--unit=foreman-{ctx.session.id}",
                    f"--working-directory={ctx.worktree}",
                    "--property=RuntimeMaxSec=1200",
                    "--service-type=exec", "bash",
                    str(ctx.pid_path.parent / "run.sh")]
    assert "muse exec" in muse_pool.inner_command(ctx)


def test_muse_detach_reads_config_file(env, monkeypatch):
    ctx = make_ctx(env)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    config_dir = Path(str(paths.config_dir()))
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "foreman.toml").write_text(
        "[launch]\nwindow_launcher = \"cfg-launcher\"\n", encoding="utf-8")
    assert muse_pool.window_launcher() == "cfg-launcher"
    assert muse_pool.outer_argv(make_ctx(env, window=True))[0] == "cfg-launcher"
    monkeypatch.setenv("FOREMAN_WINDOW_LAUNCHER", "env-launcher")
    assert muse_pool.window_launcher() == "env-launcher"


def test_muse_launch_returns_the_pid_file(env, monkeypatch):
    from foreman.pools import muse as muse_module

    ctx = make_ctx(env)
    ctx.pid_path.parent.mkdir(parents=True, exist_ok=True)

    class FakePopen:
        def __init__(self, argv, **kwargs):
            # A pid that differs from the spawn's own, proving launch reads
            # the pid file back instead of returning the launcher pid.
            ctx.pid_path.write_text("424242\n", encoding="utf-8")
            self.pid = 1

    monkeypatch.setattr(muse_module, "Popen", FakePopen)
    assert MuseAdapter().launch(ctx) == 424242


def test_muse_observe_reports_marker_and_mtime(env):
    session = Session(id="ses-obs1", role="muse", pool="muse",
                      model=MuseAdapter.model, pid=99999999, state="running")
    missing = MuseAdapter().observe(session)
    assert missing == {"transcript_mtime": None, "cpu_s": 0.0,
                       "finish_present": False, "finish_rc": None}

    log = paths.session_log_path("ses-obs1")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("work\n### finished rc=0\n", encoding="utf-8")
    seen = MuseAdapter().observe(session)
    assert seen["finish_present"] is True
    assert seen["finish_rc"] == 0
    assert isinstance(seen["transcript_mtime"], float)
    assert seen["cpu_s"] == 0.0


def test_registry_lookup_names_known_pools():
    assert isinstance(get_pool("muse"), MuseAdapter)
    with pytest.raises(ValueError, match="unknown pool"):
        get_pool("no-such-pool")


def _seed_session(sid: str, role: str) -> None:
    store.write_snapshot(
        paths.roster_path(),
        {"sessions": {sid: Session(
            id=sid, role=role, pool="fake", model="fake-test-model",
            state="running").to_dict()}},
    )


def _roster_sessions() -> dict:
    return json.loads(paths.roster_path().read_text(encoding="utf-8"))["sessions"]


def test_concurrent_launches_keep_both_sessions(env, fake_pool, capsys,
                                               monkeypatch):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    _seed_session("ses-seed0001", "muse")
    # A launch reading a stale roster must not discard the earlier entry:
    # every read-modify-write goes through the snapshot lock.
    monkeypatch.setattr(
        store, "read_snapshot", lambda *args, **kwargs: {"sessions": {}})
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) == 0
    sid = session_line(capsys.readouterr().out)
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert set(roster["sessions"]) == {"ses-seed0001", sid}


def test_dry_run_leaves_roster_untouched(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    _seed_session("ses-seed0002", "muse")
    before = paths.roster_path().read_bytes()
    worktree = str(env / "wt-dry-untouched")
    assert launch(["muse", "muse", spec, "--repo", str(repo),
                   "--worktree", worktree, "--dry-run"]) == 0
    capsys.readouterr()
    assert paths.roster_path().read_bytes() == before
    assert (Path(worktree) / "FOREMAN-JOB.md").is_file()


def test_worker_receives_role_prompt(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    worktree = env / "wt-role"
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--worktree", str(worktree)]) == 0
    capsys.readouterr()
    job_text = (worktree / "FOREMAN-JOB.md").read_text(encoding="utf-8")
    assert "You are a Foreman worker" in job_text
    assert "no Foreman tools" in job_text


@pytest.mark.parametrize("log_text, present, rc", [
    ("quoting ### finished mid-sentence\n", False, None),
    ("the line `### finished rc=$?` is written to this file\n", False, None),
    ("### finished\n", False, None),
    ("### finished rc=oops\n", False, None),
    ("work\n### finished rc=0\n", True, 0),
    ("### finished rc=2\ntrailing noise\n", True, 2),
    ("### finished rc=1\n### finished rc=3\n", True, 3),
])
def test_finish_marker_needs_own_line_with_rc(env, log_text, present, rc):
    session = Session(id="ses-marker1", role="muse", pool="muse",
                      model=MuseAdapter.model, pid=None, state="running")
    log = paths.session_log_path("ses-marker1")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(log_text, encoding="utf-8")
    seen = MuseAdapter().observe(session)
    assert seen["finish_present"] is present
    assert seen["finish_rc"] == rc


def test_launch_records_process_group(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) == 0
    sid = session_line(capsys.readouterr().out)
    record = _roster_sessions()[sid]
    assert record["pid"] == os.getpid()
    assert record["pgid"] == record["pid"]
    assert muse_pool.inner_command(make_ctx(env)).startswith("bash -c ")


@pytest.mark.parametrize("name", ["FOREMAN-JOB.md", "FOREMAN-ROLE.md"])
def test_symlinked_worktree_file_refused(env, fake_pool, capsys, name):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    target = env / "outside.txt"
    target.write_text("keep\n", encoding="utf-8")
    os.symlink(str(target), str(repo / name))
    for argv in (["add", name], ["commit", "-qm", "symlink"]):
        subprocess.run(["git", *argv], cwd=repo, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True)
    worktree = env / "wt-link"
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--worktree", str(worktree)]) != 0
    err = capsys.readouterr().err
    assert name in err
    assert "symlink" in err
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert not paths.roster_path().exists()


def test_frozen_launcher_refuses_by_name(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    frozen = paths.frozen_path()
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_text("held by the owner\n", encoding="utf-8")
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) != 0
    assert str(frozen) in capsys.readouterr().err
    assert not paths.roster_path().exists()


@pytest.mark.parametrize("role, allowed", [
    ("muse", False),
    ("supervisor", True),
    ("foreman", True),
])
def test_launch_checks_caller_role(env, fake_pool, capsys, monkeypatch,
                                   role, allowed):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    _seed_session("ses-caller01", role)
    monkeypatch.setenv("FOREMAN_SESSION", "ses-caller01")
    rc = launch(["muse", "fake", spec, "--repo", str(repo)])
    err = capsys.readouterr().err
    if allowed:
        assert rc == 0
    else:
        assert rc != 0
        assert "may not call 'launch'" in err


def test_launch_refuses_unknown_session(env, fake_pool, capsys, monkeypatch):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    monkeypatch.setenv("FOREMAN_SESSION", "ses-nope0000")
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) != 0
    assert "unknown session" in capsys.readouterr().err


def test_prespawn_failure_removes_worktree_and_branch(env, fake_pool, capsys,
                                                      monkeypatch):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)

    def boom(*args, **kwargs):
        raise launch_module.Refused("no template today")

    monkeypatch.setattr(launch_module, "render_role_template", boom)
    worktree = env / "wt-orphan"
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--worktree", str(worktree)]) != 0
    capsys.readouterr()
    assert not worktree.exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "foreman/*"], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout
    assert branches.strip() == ""
    assert not paths.roster_path().exists()


def test_failed_spawn_records_failed_session(env, fake_pool, capsys):
    from foreman import pools

    class FailAdapter(PoolAdapter):
        name = "fail"
        model = "fail-model"
        timeout_default = "5m"
        interactive = False

        def launch(self, ctx: LaunchContext) -> int:
            raise RuntimeError("no process today")

        def observe(self, session: Session) -> dict:
            return {"transcript_mtime": None, "cpu_s": 0.0,
                    "finish_present": False, "finish_rc": None}

        def command_str(self, ctx: LaunchContext) -> str:
            return "fail-exec"

    pools.register("fail", FailAdapter())
    try:
        repo = make_repo(env / "repo")
        spec = write_spec(env, "spec.md", SPEC_OK)
        assert launch(["muse", "fail", spec, "--repo", str(repo)]) != 0
        assert "failed to start" in capsys.readouterr().err
        sessions = _roster_sessions()
        assert len(sessions) == 1
        record = next(iter(sessions.values()))
        assert record["state"] == "failed"
        assert record["pid"] is None
    finally:
        pools.unregister("fail")


def test_session_recorded_before_spawn(env, fake_pool, capsys):
    from foreman import pools

    seen: dict = {}

    class WatchingAdapter(PoolAdapter):
        name = "watch"
        model = "watch-model"
        timeout_default = "5m"
        interactive = False

        def launch(self, ctx: LaunchContext) -> int:
            roster = json.loads(
                paths.roster_path().read_text(encoding="utf-8"))
            entry = roster["sessions"][ctx.session.id or ""]
            seen["pid"] = entry["pid"]
            seen["state"] = entry["state"]
            pid = os.getpid()
            ctx.pid_path.write_text(f"{pid}\n", encoding="utf-8")
            return pid

        def observe(self, session: Session) -> dict:
            return {"transcript_mtime": None, "cpu_s": 0.0,
                    "finish_present": False, "finish_rc": None}

        def command_str(self, ctx: LaunchContext) -> str:
            return "watch-exec"

    pools.register("watch", WatchingAdapter())
    try:
        repo = make_repo(env / "repo")
        spec = write_spec(env, "spec.md", SPEC_OK)
        assert launch(["muse", "watch", spec, "--repo", str(repo)]) == 0
        sid = session_line(capsys.readouterr().out)
        assert seen == {"pid": None, "state": "starting"}
        record = _roster_sessions()[sid]
        assert record["state"] == "running"
        assert record["pid"] == os.getpid()
    finally:
        pools.unregister("watch")


def test_observe_reads_session_log_path(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    elsewhere = env / "logs" / "elsewhere.log"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--log", str(elsewhere)]) == 0
    sid = session_line(capsys.readouterr().out)
    elsewhere.write_text("output\n### finished rc=0\n", encoding="utf-8")
    session = Session.from_dict(_roster_sessions()[sid])
    seen = MuseAdapter().observe(session)
    assert seen["finish_present"] is True
    assert seen["finish_rc"] == 0
    assert isinstance(seen["transcript_mtime"], float)


def test_timeout_and_worktree_persisted_on_session(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    worktree = str(env / "wt-persist")
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--worktree", worktree, "--timeout", "42m"]) == 0
    sid = session_line(capsys.readouterr().out)
    record = _roster_sessions()[sid]
    assert record["timeout"] == "42m"
    assert record["worktree"] == worktree
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) == 0
    second = session_line(capsys.readouterr().out)
    assert _roster_sessions()[second]["timeout"] == "5m"


def test_only_swarm_and_own_rulings_injected(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "spec.md", SPEC_OK)
    store.append_ledger(paths.rulings_path(),
                        {"scope": "swarm", "text": "swarm rule one",
                         "source": "owner"})
    store.append_ledger(paths.rulings_path(),
                        {"scope": "corpus", "text": "corpus rule one",
                         "source": "foreman"})
    store.append_ledger(paths.rulings_path(),
                        {"scope": "other", "text": "other rule one",
                         "source": "foreman"})
    worktree = env / "wt-rulings"
    assert launch(["muse", "fake", spec, "--repo", str(repo),
                   "--worktree", str(worktree),
                   "--front", "corpus"]) == 0
    capsys.readouterr()
    job_text = (worktree / "FOREMAN-JOB.md").read_text(encoding="utf-8")
    assert "swarm rule one" in job_text
    assert "corpus rule one" in job_text
    assert "other rule one" not in job_text


def test_no_verification_sentence_refused(env, fake_pool, capsys):
    repo = make_repo(env / "repo")
    spec = write_spec(env, "nvc2.md",
                      "Do the thing.\nNo verification is required.\n")
    assert launch(["muse", "fake", spec, "--repo", str(repo)]) != 0
    assert "verification" in capsys.readouterr().err
