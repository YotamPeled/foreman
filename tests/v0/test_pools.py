"""Grok and headless-Claude pool adapters: command lines, wrapper, usage.

No test here runs a vendor CLI. Where the wrapper itself is exercised, a
stub shell script named ``grok``/``claude`` is put on PATH in a temp dir —
the seam below the code under test — and the real ``bash -c`` wrapper runs
against it, proving the pid file is written and the finish marker reaches
the log. Expected command lines are hand-derived literals from the proven
shapes, never built with the adapters' own helpers.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from foreman import cli, paths
from foreman.entities import Session
from foreman.pools import LaunchContext, get as get_pool
from foreman.pools import claude as claude_pool
from foreman.pools import grok as grok_pool
from foreman.pools import muse as muse_pool
from foreman.pools.claude import ClaudeAdapter
from foreman.pools.grok import GrokAdapter
from foreman.pools.muse import MuseAdapter

SPEC_OK = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v0 -q\n"
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    return tmp_path


def make_ctx(tmp: Path, pool: str = "grok", kind: str = "implement",
             effort: str = "high", sid: str = "ses-test123",
             window: bool = False, role: str | None = None) -> LaunchContext:
    session = Session(id=sid, role=role or pool, pool=pool,
                      model=get_pool(pool).model, state="running")
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
        effort=effort,
        kind=kind,
        window=window,
    )


def denies_of(argv: list[str]) -> list[str]:
    """Values of every --deny flag, in order. A missing deny fails loudly."""
    return [argv[i + 1] for i, part in enumerate(argv) if part == "--deny"]


def write_stub(bindir: Path, name: str, body: str,
               monkeypatch: pytest.MonkeyPatch) -> None:
    """A stub vendor binary on PATH: a shell script, never the vendor CLI."""
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / name
    stub.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


@pytest.mark.parametrize("effort", ["high", "medium"])
def test_grok_argv_is_the_proven_shape(env, effort):
    """A dropped or reordered flag is a dead or mis-scoped launch."""
    ctx = make_ctx(env, effort=effort)
    assert grok_pool.grok_argv(ctx) == [
        "grok",
        "--prompt-file", str(ctx.job_path),
        "-m", "grok-4.6",
        "--reasoning-effort", effort,
        "--permission-mode", "bypassPermissions",
        "--deny", "MCPTool(foreman__*)",
        "--deny", "MCPTool(boxes__*)",
        "--disable-web-search",
        "--output-format", "streaming-json",
        "--cwd", str(ctx.worktree),
    ]
    assert "json" not in grok_pool.grok_argv(ctx)


@pytest.mark.parametrize("kind", ["implement", "review"])
def test_grok_launch_always_denies_swarm_tools(env, kind):
    """Standing owner ruling: no Grok launch without both swarm denies.

    The board server answers to any session id, so a launch missing either
    deny lets a worker reach swarm state. Fails if either pattern is ever
    removed from either variant.
    """
    argv = grok_pool.grok_argv(make_ctx(env, kind=kind))
    denies = denies_of(argv)
    assert "MCPTool(foreman__*)" in denies
    assert "MCPTool(boxes__*)" in denies


def test_grok_reviewer_denies_writes(env):
    """A reviewer without the three write denials can write: no sandbox
    or permission-mode flag stops it, only these denials do."""
    implement = denies_of(grok_pool.grok_argv(make_ctx(env, kind="implement")))
    assert implement == ["MCPTool(foreman__*)", "MCPTool(boxes__*)"]
    review = denies_of(grok_pool.grok_argv(make_ctx(env, kind="review")))
    assert review == ["MCPTool(foreman__*)", "MCPTool(boxes__*)",
                        "Write", "Edit", "Bash"]


def test_grok_kind_selects_reviewer_in_printed_command(env):
    """The job kind must reach the command, or review jobs launch as
    writers while reading a branch that is read-only to them."""
    review_cmd = GrokAdapter().command_str(make_ctx(env, kind="review"))
    assert "--deny Write" in review_cmd
    assert "--deny Edit" in review_cmd
    assert "--deny Bash" in review_cmd
    implement_cmd = GrokAdapter().command_str(make_ctx(env, kind="implement"))
    assert "--deny Write" not in implement_cmd
    assert "--deny Edit" not in implement_cmd
    assert " --deny Bash" not in implement_cmd


def test_grok_wrapper_writes_pid_and_marker_to_log(env, monkeypatch):
    """Marker echoed after the pipe never reaches the log; the collector
    would wait forever. Runs the real wrapper against a stub binary."""
    ctx = make_ctx(env)
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    write_stub(env / "bin", "grok", "echo vendor-output\nexit 3\n", monkeypatch)
    inner = grok_pool.inner_command(ctx)
    assert inner.startswith("bash -c ")
    assert "### finished rc=$?" in inner
    assert inner.index("### finished rc=$?") < inner.index("| tee")
    subprocess.run(["bash", "-c", inner], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert int(ctx.pid_path.read_text(encoding="utf-8").strip()) > 0
    lines = ctx.log_path.read_text(encoding="utf-8").splitlines()
    assert "vendor-output" in lines
    assert "### finished rc=3" in lines


def test_grok_inner_exports_claude_family_switches(env, monkeypatch):
    """A grok that reads the owner's Claude skills is the launch missing
    these six; they must be exported as false in the inner command itself
    so a unit, a window and a dry run all carry them."""
    ctx = make_ctx(env)
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    names = grok_pool.CLAUDE_FAMILY_SWITCHES
    assert names == (
        "GROK_CLAUDE_SKILLS_ENABLED",
        "GROK_CURSOR_SKILLS_ENABLED",
        "GROK_CLAUDE_AGENTS_ENABLED",
        "GROK_CLAUDE_RULES_ENABLED",
        "GROK_CLAUDE_MCPS_ENABLED",
        "GROK_CLAUDE_HOOKS_ENABLED",
    )
    body = "".join(f'printf "%s\\n" "${{{name}}}"\n' for name in names)
    write_stub(env / "bin", "grok", body + "exit 0\n", monkeypatch)
    inner = grok_pool.inner_command(ctx)
    vendor_at = inner.index("grok --prompt-file")
    for name in names:
        token = f"{name}=false"
        assert token in inner
        assert inner.index(token) < vendor_at
    subprocess.run(["bash", "-c", inner], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = ctx.log_path.read_text(encoding="utf-8").splitlines()
    assert lines[:6] == ["false"] * 6
    assert lines[-1] == "### finished rc=0"


def test_grok_streaming_log_writes_before_the_finish_marker(env, monkeypatch):
    """`--output-format json` buffered until exit, so a supervisor watching
    a long job saw an empty log. A stub that prints two NDJSON lines then
    exits must leave those lines in the log before the finish marker."""
    ctx = make_ctx(env)
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    write_stub(
        env / "bin", "grok",
        "printf '%s\\n' '{\"type\":\"update\"}' "
        "'{\"type\":\"result\",\"usage\":"
        "{\"input_tokens\":1,\"output_tokens\":2}}'\n"
        "exit 0\n",
        monkeypatch,
    )
    inner = grok_pool.inner_command(ctx)
    subprocess.run(["bash", "-c", inner], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = ctx.log_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"type":"update"}'
    assert lines[-1] == "### finished rc=0"
    assert lines[0] != lines[-1]


def test_grok_launch_returns_the_pid_file(env, monkeypatch):
    """Returning the spawner's pid instead of the worker's would aim a
    later kill at the wrong process group."""
    from foreman.pools import grok as grok_module

    ctx = make_ctx(env)
    ctx.pid_path.parent.mkdir(parents=True, exist_ok=True)

    class FakePopen:
        def __init__(self, argv, **kwargs):
            ctx.pid_path.write_text("424243\n", encoding="utf-8")
            self.pid = 1

    monkeypatch.setattr(grok_module, "Popen", FakePopen)
    assert GrokAdapter().launch(ctx) == 424243


def test_claude_argv_denies_swarm_tools_with_bare_binary(env):
    """The vendor binary is found on PATH, never by absolute path; the
    swarm's tools are denied or a worker can touch swarm state; and the
    model and the permission mode are named rather than inherited from
    whatever this machine happens to be configured with (owner ruling,
    2026-09-08: nothing is guessed at launch)."""
    ctx = make_ctx(env, pool="claude")
    argv = claude_pool.claude_argv(ctx)
    assert argv == [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", claude_pool.model_for(ctx.session.role),
        "--dangerously-skip-permissions",
        "--disallowedTools", "mcp__foreman__* mcp__boxes__*",
        "--add-dir", str(ctx.worktree),
    ]
    assert "/" not in argv[0]


def test_claude_argv_uses_the_session_model_when_set(env):
    """A session that already carries a model is started with it, not
    with the role pin. A user pool wrapping this adapter records its
    manifest model on the session; without this read, every such pool
    would launch claude-opus-5."""
    from dataclasses import replace

    ctx = make_ctx(env, pool="claude", role="opus")
    ctx = replace(ctx, session=replace(ctx.session, model="claude-fable-5-1"))
    argv = claude_pool.claude_argv(ctx)
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"


def test_claude_argv_falls_back_to_the_role_pin_when_the_session_has_none(env):
    """A session that carries no model still gets the role pin, never
    the machine default."""
    from dataclasses import replace

    ctx = make_ctx(env, pool="claude", role="opus")
    ctx = replace(ctx, session=replace(ctx.session, model=""))
    argv = claude_pool.claude_argv(ctx)
    assert argv[argv.index("--model") + 1] == "claude-opus-5"


def test_claude_worker_names_model_directory_and_permission_mode(env):
    """All three things a launch must not leave to the machine: the model
    for the role, the worktree as the working directory, and the permission
    mode. A worker that inherits the launcher's directory is one relative
    path away from editing the wrong checkout, and a headless worker has
    nobody to answer an approval prompt."""
    ctx = make_ctx(env, pool="claude", role="opus")
    inner = claude_pool.inner_command(ctx)
    assert "--model claude-opus-5" in inner
    assert "--dangerously-skip-permissions" in inner
    assert f"--working-directory={ctx.worktree}" in " ".join(
        claude_pool.outer_argv(ctx))
    assert claude_pool.model_for("opus") == "claude-opus-5"
    assert claude_pool.model_for("") == claude_pool.MODEL


def test_claude_wrapper_feeds_spec_and_transcribes(env, monkeypatch):
    """The spec must reach the vendor on stdin and the JSONL transcript
    must land in the log, or usage has nothing to read later. Runs the
    real wrapper against a stub binary."""
    ctx = make_ctx(env, pool="claude")
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.job_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.job_path.write_text("WHAT: judge this branch.\n", encoding="utf-8")
    write_stub(env / "bin", "claude",
               "cat\n"
               "printf '%s\\n' '{\"type\":\"result\","
               "\"usage\":{\"input_tokens\":11,\"output_tokens\":7}}'\n"
               "exit 5\n",
               monkeypatch)
    inner = claude_pool.inner_command(ctx)
    assert inner.startswith("bash -c ")
    assert f"< {ctx.job_path}" in inner
    assert inner.index("### finished rc=$?") < inner.index("| tee")
    subprocess.run(["bash", "-c", inner], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert int(ctx.pid_path.read_text(encoding="utf-8").strip()) > 0
    text = ctx.log_path.read_text(encoding="utf-8")
    assert "WHAT: judge this branch." in text
    assert '"input_tokens":11' in text
    assert "### finished rc=5" in text.splitlines()


def test_claude_usage_reports_transcript_tokens(env):
    """Wrong totals would bill the wrong model. The last usage object in
    the transcript carries the run's totals."""
    session = Session(id="ses-usage1", role="grok", pool="claude",
                      model=ClaudeAdapter.model, pid=None, state="running")
    log = paths.session_log_path("ses-usage1")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        '{"type":"assistant","message":{"usage":{"input_tokens":100,"output_tokens":10}}}\n'
        '{"type":"result","usage":{"input_tokens":111,"output_tokens":17}}\n',
        encoding="utf-8",
    )
    assert ClaudeAdapter().usage(session) == {
        "input_tokens": 111, "output_tokens": 17}


@pytest.mark.parametrize("log_text", [
    "plain prose, no JSON at all\n",
    '{"type":"result","result":"done"}\n',
    '{"type":"assistant","message":{"usage":{"input_tokens":"many"}}}\n',
    'not json\n{"broken": \n',
])
def test_claude_usage_is_nothing_without_tokens(env, log_text):
    """No usable usage object means no number, never a zero or a crash
    taking down the collector's tick."""
    session = Session(id="ses-usage2", role="grok", pool="claude",
                      model=ClaudeAdapter.model, pid=None, state="running")
    log = paths.session_log_path("ses-usage2")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(log_text, encoding="utf-8")
    assert ClaudeAdapter().usage(session) is None


def test_claude_usage_missing_transcript_is_nothing(env):
    session = Session(id="ses-usage3", role="grok", pool="claude",
                      model=ClaudeAdapter.model, pid=None, state="running")
    assert ClaudeAdapter().usage(session) is None


@pytest.mark.parametrize("pool,adapter", [
    ("grok", GrokAdapter()),
    ("claude", ClaudeAdapter()),
])
@pytest.mark.parametrize("log_text", [
    "the spec says write `### finished rc=$?` as the last line\n",
    "> ### finished rc=0 (quoted from the runbook)\n",
    'judge wrote "### finished rc=1" in its prose summary\n',
])
def test_finish_marker_in_quoted_prose_is_not_finished(env, pool, adapter,
                                                       log_text):
    """A log whose only marker mention sits inside quoted prose must not
    read as finished, or a still-running job is marked returned."""
    session = Session(id="ses-prose1", role="grok", pool=pool,
                      model=adapter.model, pid=None, state="running")
    log = paths.session_log_path("ses-prose1")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(log_text, encoding="utf-8")
    seen = adapter.observe(session)
    assert seen["finish_present"] is False
    assert seen["finish_rc"] is None


@pytest.mark.parametrize("pool,adapter", [
    ("grok", GrokAdapter()),
    ("claude", ClaudeAdapter()),
])
def test_observe_reports_real_marker(env, pool, adapter):
    """A real marker line is still completion: the prose rule must not
    swallow the signal it guards."""
    session = Session(id="ses-prose2", role="grok", pool=pool,
                      model=adapter.model, pid=99999999, state="running")
    log = paths.session_log_path("ses-prose2")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("work\n### finished rc=2\n", encoding="utf-8")
    seen = adapter.observe(session)
    assert seen == {"transcript_mtime": seen["transcript_mtime"],
                    "cpu_s": 0.0, "finish_present": True, "finish_rc": 2}
    assert isinstance(seen["transcript_mtime"], float)


def test_pools_without_counters_report_no_usage(env):
    """Grok and Muse publish no token counter: the honest answer is None,
    never an invented number."""
    session = Session(id="ses-nousage", role="muse", pool="muse",
                      model=MuseAdapter.model, pid=None, state="running")
    assert GrokAdapter().usage(session) is None
    assert MuseAdapter().usage(session) is None


@pytest.mark.parametrize("adapter", [GrokAdapter(), ClaudeAdapter()])
@pytest.mark.parametrize("verdict_file,expected", [
    ('{"verdict": "pass", "summary": "clean read"}',
     {"passed": True, "summary": "clean read"}),
    ('{"result": "failed", "detail": "two findings"}',
     {"passed": False, "summary": "two findings"}),
    ('{"passed": true}', {"passed": True, "summary": ""}),
    ('{"status": "OK"}', {"passed": True, "summary": ""}),
])
def test_verdict_normalises_pass_fail(env, adapter, verdict_file, expected):
    """Two pools must never disagree about the same review: one shared
    reading, normalised to pass/fail plus summary."""
    path = env / "verdict.json"
    path.write_text(verdict_file, encoding="utf-8")
    assert adapter.verdict(path) == expected


@pytest.mark.parametrize("adapter", [GrokAdapter(), ClaudeAdapter()])
@pytest.mark.parametrize("verdict_file", [
    '{"verdict": "maybe"}',
    '{"summary": "no verdict named"}',
    "[1, 2]",
    "not json at all",
])
def test_verdict_refuses_guessing(env, adapter, verdict_file):
    """A verdict file naming no verdict must fail loudly: a silent default
    would land a task or send it back on no evidence."""
    path = env / "verdict.json"
    path.write_text(verdict_file, encoding="utf-8")
    with pytest.raises(ValueError):
        adapter.verdict(path)


@pytest.mark.parametrize("adapter", [GrokAdapter(), ClaudeAdapter()])
def test_verdict_missing_file_is_an_error(env, adapter):
    with pytest.raises(ValueError, match="unreadable"):
        adapter.verdict(env / "no-such-verdict.json")


def test_registry_resolves_all_pools():
    """The launcher resolves pools by name: an unregistered adapter is an
    'unknown pool' refusal at launch."""
    assert isinstance(get_pool("muse"), MuseAdapter)
    assert isinstance(get_pool("grok"), GrokAdapter)
    assert isinstance(get_pool("claude"), ClaudeAdapter)
    with pytest.raises(ValueError, match="unknown pool"):
        get_pool("no-such-pool")


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


def launch(argv: list[str]) -> int:
    return cli.main(["launch", *argv])


def session_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("session: "):
            return line.split("session: ", 1)[1].strip()
    raise AssertionError(f"no session line in output:\n{out}")


def test_launch_dry_run_prints_grok_claude_family_switches(env, capsys):
    """A dry run that hides the six switches would launch a grok that
    still reads the owner's Claude skills: the printed command is the
    one a unit and a window would run."""
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    rc = launch(["grok", "grok", str(spec), "--repo", str(repo),
                 "--worktree", str(env / "wt-switches"), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    vendor_at = out.index("grok --prompt-file")
    for name in grok_pool.CLAUDE_FAMILY_SWITCHES:
        token = f"{name}=false"
        assert token in out
        assert out.index(token) < vendor_at


def test_launch_dry_run_accepts_medium_effort(env, capsys):
    """Grok's documented labeling effort is medium; the launcher offering
    only high|xhigh would refuse it before the adapter ever sees it."""
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    rc = launch(["grok", "grok", str(spec), "--repo", str(repo),
                 "--worktree", str(env / "wt-medium"),
                 "--effort", "medium", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--reasoning-effort medium" in out
    assert "--deny" in out and "foreman__*" in out and "boxes__*" in out


def test_launch_dry_run_propagates_review_kind(env, capsys):
    """A review job planned through the launcher must print the reviewer
    denials: the kind has to travel from argv to the adapter's command."""
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    assert launch(["grok", "grok", str(spec), "--repo", str(repo),
                   "--worktree", str(env / "wt-review"),
                   "--kind", "review", "--dry-run"]) == 0
    review_out = capsys.readouterr().out
    assert "--deny Write" in review_out
    assert launch(["grok", "grok", str(spec), "--repo", str(repo),
                   "--worktree", str(env / "wt-impl"),
                   "--dry-run"]) == 0
    implement_out = capsys.readouterr().out
    assert "--deny Write" not in implement_out


def test_muse_adapter_unchanged_shape(env):
    """The refactor onto the shared wrapper must not move the proven
    muse command: exact shape, pid first, marker inside the stream."""
    ctx = make_ctx(env, pool="muse")
    assert muse_pool.muse_argv(ctx) == [
        "muse", "exec",
        "--model", "muse-spark-1.3-contributor",
        "--reasoning-effort", "high",
        "--approval-mode", "never",
        "--json",
        "--workspace", str(ctx.worktree),
        "--prompt-file", str(ctx.job_path),
    ]
    inner = muse_pool.inner_command(ctx)
    assert inner.index("### finished rc=$?") < inner.index("| tee")


def test_vendor_binaries_resolve_on_path(env):
    """Public-repo rule: bare binary names, never an absolute path into
    any machine's filesystem."""
    for argv in (grok_pool.grok_argv(make_ctx(env)),
                 claude_pool.claude_argv(make_ctx(env, pool="claude")),
                 muse_pool.muse_argv(make_ctx(env, pool="muse"))):
        assert "/" not in argv[0]
        assert not Path(argv[0]).is_absolute()


@pytest.mark.parametrize("pool,module", [
    ("muse", muse_pool), ("grok", grok_pool), ("claude", claude_pool)])
def test_a_configured_launcher_alone_opens_no_window(env, monkeypatch, pool,
                                                     module):
    """Owner ruling: swarm sessions take no desktop workspace by themselves.

    Configuring a window launcher must not be enough; only a launch that
    asked to be watched gets a window. Every pool, not just the first one
    written, because the shared wrapper is where this can quietly regress.
    """
    monkeypatch.setenv("FOREMAN_WINDOW_LAUNCHER", "test-launcher")
    headless = module.outer_argv(make_ctx(env, pool=pool))
    assert headless[0] == "systemd-run"
    watched = module.outer_argv(make_ctx(env, pool=pool, window=True))
    assert watched[0] == "test-launcher"


def test_headless_spawn_survives_its_own_systemd_unit(env):
    """The worker shell is the unit's main process (--service-type=exec),
    so unit lifetime is job lifetime with no setsid workaround; and the
    unit is never collected, so a finished unit still answers its exit
    status instead of reading as success.
    """
    inner = muse_pool.inner_command(make_ctx(env, pool="muse"))
    assert inner.startswith("bash -c ")
    assert "setsid" not in inner
    argv = muse_pool.outer_argv(make_ctx(env, pool="muse"))
    assert argv[0] == "systemd-run" and "--collect" not in argv
    assert "--service-type=exec" in argv
