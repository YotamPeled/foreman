"""Launcher and Muse adapter: fake-pool launches, refusals, dry-run."""

import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

from foreman import cli, paths, store
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
                 "--worktree", worktree, "--component", "corpus",
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
    assert '# Job job-1 \u00b7 implement \u00b7 task "second reads" \u00b7 component corpus' in job_text
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
    assert record["component"] == "corpus"
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
    assert "--yolo" in out
    assert f"--workspace {worktree}" in out
    assert "--prompt-file" in out and "FOREMAN-JOB.md" in out
    assert "### finished rc=$?" in out
    assert out.index("### finished rc=$?") < out.index("| tee")
    assert "(not started --dry-run)" in out
    # Dry-run does everything except start: files and roster exist, no pid.
    assert (Path(worktree) / "FOREMAN-JOB.md").is_file()
    assert (Path(worktree) / "FOREMAN-ROLE.md").is_file()
    assert not paths.session_pid_path(sid).exists()
    roster = json.loads(paths.roster_path().read_text(encoding="utf-8"))
    assert roster["sessions"][sid]["pid"] is None


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
    capsys.readouterr()
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


def make_ctx(tmp: Path, sid: str = "ses-test123") -> LaunchContext:
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
    )


def test_muse_argv_is_exactly_the_proven_shape(env):
    ctx = make_ctx(env)
    assert muse_pool.muse_argv(ctx) == [
        "muse", "exec",
        "--model", "muse-spark-1.3-contributor",
        "--reasoning-effort", "high",
        "--yolo",
        "--workspace", str(ctx.worktree),
        "--prompt-file", str(ctx.job_path),
    ]


def test_muse_marker_stays_inside_the_redirection(env):
    ctx = make_ctx(env)
    inner = muse_pool.inner_command(ctx)
    assert "### finished rc=$?" in inner
    assert inner.index("### finished rc=$?") < inner.index("| tee")
    assert str(ctx.pid_path) in inner
    assert str(ctx.log_path) in inner


def test_muse_detach_prefers_window_launcher(env, monkeypatch):
    ctx = make_ctx(env)
    monkeypatch.setenv("FOREMAN_WINDOW_LAUNCHER", "test-launcher")
    argv = muse_pool.outer_argv(ctx)
    assert argv[:4] == ["test-launcher", f"foreman-{ctx.session.id}",
                        "bash", "-c"]
    assert "muse exec" in argv[4]


def test_muse_detach_defaults_to_systemd_run(env, monkeypatch):
    ctx = make_ctx(env)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    argv = muse_pool.outer_argv(ctx)
    assert argv[:6] == ["systemd-run", "--user", "--collect",
                        f"--unit=foreman-{ctx.session.id}", "bash", "-c"]
    assert "muse exec" in argv[6]


def test_muse_detach_reads_config_file(env, monkeypatch):
    ctx = make_ctx(env)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    config_dir = Path(str(paths.config_dir()))
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "foreman.toml").write_text(
        "[launch]\nwindow_launcher = \"cfg-launcher\"\n", encoding="utf-8")
    assert muse_pool.window_launcher() == "cfg-launcher"
    assert muse_pool.outer_argv(ctx)[0] == "cfg-launcher"
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
                       "finish_present": False}

    log = paths.session_log_path("ses-obs1")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("work\n### finished rc=0\n", encoding="utf-8")
    seen = MuseAdapter().observe(session)
    assert seen["finish_present"] is True
    assert isinstance(seen["transcript_mtime"], float)
    assert seen["cpu_s"] == 0.0


def test_registry_lookup_names_known_pools():
    assert isinstance(get_pool("muse"), MuseAdapter)
    with pytest.raises(ValueError, match="unknown pool"):
        get_pool("no-such-pool")
