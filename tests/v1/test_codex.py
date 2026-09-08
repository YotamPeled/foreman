"""The ``codex`` pool adapter: vendor argv, trust edit, launch, verdict.

No test here runs the real ``codex`` binary except the smoke test at
the bottom. Where the wrapper itself is exercised, a stub shell script
named ``codex`` is put on PATH in a temp dir — the seam below the code
under test — and the real ``bash -c`` wrapper runs against it, proving
the pid file is written, the job file arrives on stdin, and the finish
marker reaches the log. Expected command lines are hand-derived
literals from the verified shape, never built with the adapter's own
helpers. ``HOME``, ``FOREMAN_STATE`` and ``FOREMAN_CONFIG`` point at a
tmp dir throughout, so the trust edit never touches the real config.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from foreman import cli, paths, store
from foreman.entities import Session
from foreman.pools import LaunchContext, get as get_pool
from foreman.pools import codex as codex_pool
from foreman.pools import codex as codex_module
from foreman.pools.codex import CodexAdapter

SPEC_OK = (
    "WHAT: label ten apps.\n"
    "INPUTS: manifest.json.\n"
    "OUTPUTS: labels/ten.json.\n"
    "OUT OF SCOPE: everything else.\n"
    "Run the check with: pytest tests/v1 -q\n"
)

#: The checkout this test file sits in: the schema path below is derived
#: from it, never from the adapter's own ``schema_path`` helper.
REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SCHEMA = (REPO_ROOT / "src" / "foreman" / "pools"
                   / "codex_verdict.schema.json")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return tmp_path


def make_ctx(tmp: Path, pool: str = "codex", kind: str = "implement",
             effort: str = "high", sid: str = "ses-test123",
             window: bool = False) -> LaunchContext:
    session = Session(id=sid, role="astra", pool=pool,
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
        timeout="25m",
        effort=effort,
        kind=kind,
        window=window,
    )


def write_stub(bindir: Path, name: str, body: str,
               monkeypatch: pytest.MonkeyPatch) -> None:
    """A stub vendor binary on PATH: a shell script, never the vendor CLI."""
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / name
    stub.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


def test_codex_argv_is_the_verified_shape(env):
    """A dropped or reordered flag is a dead or mis-scoped launch.

    Breaks caught: the model flag renamed, the effort override passed as
    a flag instead of ``-c``, the bypass or repo-check flag dropped, the
    working directory or schema flag dropped, ``-o`` pointed at the
    verdict file, or the prompt moved off stdin onto the command line.
    """
    ctx = make_ctx(env)
    assert codex_pool.schema_path() == EXPECTED_SCHEMA
    assert EXPECTED_SCHEMA.is_file()
    assert codex_pool.codex_argv(ctx) == [
        "codex",
        "exec",
        "--model", "gpt-6-astra",
        "-c", 'model_reasoning_effort="high"',
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C", str(ctx.worktree),
        "--output-schema", str(EXPECTED_SCHEMA),
        "-o", str(ctx.verdict_path.parent / "last-message.txt"),
        "-",
    ]
    assert "/" not in codex_pool.codex_argv(ctx)[0]


def test_last_message_is_beside_the_verdict_never_the_verdict(env):
    """Pointing ``-o`` at the verdict path lets the last message
    overwrite the reviewer's verdict JSON — a real review lost that way
    once, nine findings replaced by one sentence of prose."""
    ctx = make_ctx(env)
    argv = codex_pool.codex_argv(ctx)
    assert str(ctx.verdict_path) not in argv
    assert argv[argv.index("-o") + 1] != str(ctx.verdict_path)
    assert argv[argv.index("-o") + 1].endswith("last-message.txt")


@pytest.mark.parametrize("effort,want", [
    ("high", "high"),
    ("medium", "medium"),
    ("xhigh", "high"),
])
def test_effort_mapping(env, effort, want):
    """Codex has no fourth reasoning level: the launcher's ``xhigh``
    runs as ``high``. A mapping that passed ``xhigh`` through would hand
    the vendor a value it does not understand; one that flattened
    ``medium`` would overpay for every labeling run."""
    ctx = make_ctx(env, effort=effort)
    assert codex_pool.codex_effort(effort) == want
    argv = codex_pool.codex_argv(ctx)
    assert argv[argv.index("-c") + 1] == f'model_reasoning_effort="{want}"'


def test_trust_edit_creates_a_missing_config(env):
    """First launch on a machine with no Codex config must still start
    trusted: the file is created holding just the new table."""
    worktree = env / "wt"
    path = codex_pool.ensure_trust(worktree)
    assert path == Path(env / "home" / ".codex" / "config.toml")
    assert path.read_text(encoding="utf-8") == (
        f'[projects."{worktree}"]\n'
        'trust_level = "trusted"\n'
    )


def test_trust_edit_appends_only_its_table(env):
    """An existing config keeps every line byte-identical: the edit
    appends its table, never rewrites from a template. Asserts on the
    full file text, not a substring, so a reformatting edit fails."""
    worktree = env / "wt"
    path = Path(env / "home" / ".codex" / "config.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    before = (
        'model = "gpt-6-astra"\n'
        "\n"
        "[features]\n"
        "hooks = true\n"
    )
    path.write_text(before, encoding="utf-8")
    codex_pool.ensure_trust(worktree)
    assert path.read_text(encoding="utf-8") == (
        before
        + "\n"
        + f'[projects."{worktree}"]\n'
        + 'trust_level = "trusted"\n'
    )


def test_trust_edit_is_a_noop_when_the_table_exists(env):
    """A second launch must not duplicate the table or touch a byte:
    the file reads back identical down to the last newline."""
    worktree = env / "wt"
    path = Path(env / "home" / ".codex" / "config.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    before = (
        "[features]\n"
        "hooks = true\n"
        "\n"
        f'[projects."{worktree}"]\n'
        'trust_level = "trusted"\n'
    )
    path.write_text(before, encoding="utf-8")
    codex_pool.ensure_trust(worktree)
    assert path.read_text(encoding="utf-8") == before


def test_trust_edit_tolerates_a_missing_trailing_newline(env):
    """Appending after a final line with no newline must keep that
    line's content intact rather than gluing the table onto it."""
    worktree = env / "wt"
    path = Path(env / "home" / ".codex" / "config.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[other]\nkey = 1', encoding="utf-8")
    codex_pool.ensure_trust(worktree)
    assert path.read_text(encoding="utf-8") == (
        "[other]\n"
        "key = 1\n"
        "\n"
        f'[projects."{worktree}"]\n'
        'trust_level = "trusted"\n'
    )


def test_codex_wrapper_feeds_job_on_stdin(env, monkeypatch):
    """The prompt must reach the vendor on stdin and the log must carry
    the transcript: runs the real wrapper against a stub binary. A
    wrapper that put the job file on the command line, or echoed the
    marker after the pipe, would fail here the way it failed in
    production for the other pools."""
    ctx = make_ctx(env)
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.job_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.job_path.write_text("WHAT: judge this branch.\n", encoding="utf-8")
    write_stub(env / "bin", "codex", "cat\nexit 5\n", monkeypatch)
    inner = codex_pool.inner_command(ctx)
    assert inner.startswith("setsid --wait ")
    assert f"< {ctx.job_path}" in inner
    assert f"cd {ctx.worktree} &&" in inner
    assert inner.index("### finished rc=$?") < inner.index("| tee")
    subprocess.run(["bash", "-c", inner], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert int(ctx.pid_path.read_text(encoding="utf-8").strip()) > 0
    text = ctx.log_path.read_text(encoding="utf-8")
    assert "WHAT: judge this branch." in text
    assert "### finished rc=5" in text.splitlines()


def test_codex_launch_returns_the_pid_file(env, monkeypatch):
    """Returning the spawner's pid instead of the worker's would aim a
    later kill at the wrong process group."""
    ctx = make_ctx(env)
    ctx.pid_path.parent.mkdir(parents=True, exist_ok=True)

    class FakePopen:
        def __init__(self, argv, **kwargs):
            ctx.pid_path.write_text("424243\n", encoding="utf-8")
            self.pid = 1

    monkeypatch.setattr(codex_module, "Popen", FakePopen)
    assert CodexAdapter().launch(ctx) == 424243


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


class FakeSpawn:
    """Stands in for ``subprocess.Popen``: finds the worker script the
    launcher hands to systemd-run and writes this process's pid where
    the wrapper shell would, so the launch below it runs for real."""

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.pid = 1
        script = next(part for part in argv if part.endswith("run.sh"))
        Path(script).parent.joinpath("pid").write_text(
            f"{os.getpid()}\n", encoding="utf-8")


def test_launch_astra_through_codex_lands_on_the_roster(
        env, monkeypatch, capsys):
    """``foreman launch astra codex <spec>`` must mint a running session
    holding a slot: the pid comes from the pid file, the trust table is
    written, and the roster carries the session as running. Would fail
    if the pool were unregistered, the role template missing, or the
    launch recording a failed spawn as running."""
    monkeypatch.setattr(codex_module, "Popen", FakeSpawn)
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    worktree = str(env / "wt-review")

    rc = launch(["astra", "codex", str(spec), "--repo", str(repo),
                 "--worktree", worktree, "--kind", "review"])
    assert rc == 0
    out = capsys.readouterr().out
    sid = session_line(out)
    assert sid.startswith("ses-")
    assert f"pid: {os.getpid()}" in out
    assert "codex exec" in out
    assert "--output-schema" in out
    assert "last-message.txt" in out

    roster = store.read_snapshot(paths.roster_path(), default={})
    entry = roster["sessions"][sid]
    assert entry["state"] == "running"
    assert entry["pid"] == os.getpid()
    assert entry["pool"] == "codex"
    assert entry["role"] == "astra"
    assert entry["model"] == "gpt-6-astra"

    config_text = Path(env / "home" / ".codex" / "config.toml").read_text(
        encoding="utf-8")
    assert f'[projects."{worktree}"]\ntrust_level = "trusted"\n' in config_text

    job_text = Path(worktree, "FOREMAN-JOB.md").read_text(encoding="utf-8")
    assert "## Verdict schema" in job_text
    schema_text = EXPECTED_SCHEMA.read_text(encoding="utf-8").strip()
    assert schema_text in job_text


def test_launch_dry_run_writes_nothing(env, capsys):
    """A dry run prints the codex command and records nothing: no
    roster, no trust table, no worker script."""
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    rc = launch(["astra", "codex", str(spec), "--repo", str(repo),
                 "--worktree", str(env / "wt-dry"),
                 "--kind", "review", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "codex exec" in out
    assert "--output-schema" in out
    assert not paths.roster_path().exists()
    assert not Path(env / "home" / ".codex" / "config.toml").exists()


def test_launcher_still_refuses_an_unknown_role(env, capsys):
    """Registering the pool must not loosen role checking: a role no
    template knows is refused, naming the role."""
    repo = make_repo(env / "repo")
    spec = env / "spec.md"
    spec.write_text(SPEC_OK, encoding="utf-8")
    rc = launch(["no-such-role", "codex", str(spec), "--repo", str(repo),
                 "--worktree", str(env / "wt-bad"), "--dry-run"])
    assert rc == 1
    assert "unknown role 'no-such-role'" in capsys.readouterr().err


@pytest.mark.parametrize("log_text", [
    "the spec says write `### finished rc=$?` as the last line\n",
    "> ### finished rc=0 (quoted from the runbook)\n",
    'judge wrote "### finished rc=1" in its prose summary\n',
])
def test_finish_marker_in_quoted_prose_is_not_finished(env, log_text):
    """A log whose only marker mention sits inside quoted prose must not
    read as finished, or a still-running review is marked returned."""
    session = Session(id="ses-prose1", role="astra", pool="codex",
                      model=CodexAdapter.model, pid=None, state="running")
    log = paths.session_log_path("ses-prose1")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(log_text, encoding="utf-8")
    seen = CodexAdapter().observe(session)
    assert seen["finish_present"] is False
    assert seen["finish_rc"] is None


def test_observe_reports_a_real_marker(env):
    """A real marker line is still completion: the prose rule must not
    swallow the signal it guards."""
    session = Session(id="ses-prose2", role="astra", pool="codex",
                      model=CodexAdapter.model, pid=99999999, state="running")
    log = paths.session_log_path("ses-prose2")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("work\n### finished rc=2\n", encoding="utf-8")
    seen = CodexAdapter().observe(session)
    assert seen == {"transcript_mtime": seen["transcript_mtime"],
                    "cpu_s": 0.0, "finish_present": True, "finish_rc": 2}
    assert isinstance(seen["transcript_mtime"], float)


def test_usage_reports_no_meter(env):
    """Codex exposes no meter this adapter can read: the honest answer
    is None, never an invented number."""
    session = Session(id="ses-nousage", role="astra", pool="codex",
                      model=CodexAdapter.model, pid=None, state="running")
    assert CodexAdapter().usage(session) is None


@pytest.mark.parametrize("verdict_file,expected", [
    ('{"passed": true, "summary": "clean read", "findings": []}',
     {"passed": True, "summary": "clean read"}),
    ('{"passed": false, "summary": "two findings",'
     ' "findings": [{"title": "red", "detail": "is red"}]}',
     {"passed": False, "summary": "two findings"}),
    ('{"verdict": "pass", "summary": "clean read"}',
     {"passed": True, "summary": "clean read"}),
    ('{"result": "failed", "detail": "two findings"}',
     {"passed": False, "summary": "two findings"}),
])
def test_verdict_normalises_pass_fail(env, verdict_file, expected):
    """Two pools must never disagree about the same review: the codex
    verdict goes through the one shared reading, findings and all."""
    path = env / "verdict.json"
    path.write_text(verdict_file, encoding="utf-8")
    assert CodexAdapter().verdict(path) == expected


@pytest.mark.parametrize("verdict_file", [
    '{"verdict": "maybe"}',
    '{"summary": "no verdict named"}',
    "[1, 2]",
    "not json at all",
])
def test_verdict_refuses_guessing(env, verdict_file):
    """A verdict file naming no verdict must fail loudly: a silent
    default would land a task or send it back on no evidence."""
    path = env / "verdict.json"
    path.write_text(verdict_file, encoding="utf-8")
    with pytest.raises(ValueError):
        CodexAdapter().verdict(path)


def test_verdict_missing_file_is_an_error(env):
    with pytest.raises(ValueError, match="unreadable"):
        CodexAdapter().verdict(env / "no-such-verdict.json")


def test_observe_records_the_vendor_rollout(env):
    """The rollout pointer is a convenience for a person reading the
    transcript later: observe records the newest rollout no older than
    the session log, and keeps the log reading itself unchanged."""
    sid = "ses-rollout1"
    session = Session(id=sid, role="astra", pool="codex",
                      model=CodexAdapter.model, pid=99999999,
                      state="running")
    log = paths.session_log_path(sid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("work\n### finished rc=0\n", encoding="utf-8")
    rollout_dir = (Path(env / "home" / ".codex" / "sessions")
                   / "2026" / "09" / "08")
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout = rollout_dir / "rollout-2026-09-08T21-41-55-test.jsonl"
    rollout.write_text('{"x": 1}\n', encoding="utf-8")

    seen = CodexAdapter().observe(session)
    assert seen["finish_present"] is True
    pointer = paths.session_dir(sid) / "vendor-rollout"
    assert pointer.read_text(encoding="utf-8") == str(rollout) + "\n"


def test_observe_leaves_the_pointer_absent_without_a_rollout(env):
    """No rollout on disk means no pointer file: the collector must
    never depend on a transcript only Codex can write."""
    sid = "ses-rollout2"
    session = Session(id=sid, role="astra", pool="codex",
                      model=CodexAdapter.model, pid=99999999,
                      state="running")
    log = paths.session_log_path(sid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("work\n", encoding="utf-8")

    seen = CodexAdapter().observe(session)
    assert seen["finish_present"] is False
    assert not (paths.session_dir(sid) / "vendor-rollout").exists()


def test_observe_never_repoints_an_old_session(env):
    """A later tick must not re-point an old session at a newer run's
    transcript: the first pointer found wins."""
    sid = "ses-rollout3"
    session = Session(id=sid, role="astra", pool="codex",
                      model=CodexAdapter.model, pid=99999999,
                      state="running")
    log = paths.session_log_path(sid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("work\n", encoding="utf-8")
    session_dir = paths.session_dir(sid)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "vendor-rollout").write_text("/kept/rollout.jsonl\n",
                                                encoding="utf-8")
    rollout_dir = (Path(env / "home" / ".codex" / "sessions")
                   / "2026" / "09" / "08")
    rollout_dir.mkdir(parents=True, exist_ok=True)
    (rollout_dir / "rollout-newer.jsonl").write_text("{}\n",
                                                    encoding="utf-8")

    CodexAdapter().observe(session)
    assert (session_dir / "vendor-rollout").read_text(
        encoding="utf-8") == "/kept/rollout.jsonl\n"


def test_registry_resolves_codex_with_its_contract():
    """The launcher resolves pools by name; the contract is the model,
    a non-interactive pool, and the 25-minute default timeout."""
    adapter = get_pool("codex")
    assert isinstance(adapter, CodexAdapter)
    assert adapter.model == "gpt-6-astra"
    assert adapter.interactive is False
    assert adapter.timeout_default == "25m"


def test_schema_file_is_valid_json_schema():
    """``--output-schema`` is handed to the vendor as-is: the file must
    parse, describe an object, and require the verdict shape the shared
    reading understands."""
    schema = json.loads(EXPECTED_SCHEMA.read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert "passed" in schema["required"]
    assert schema["properties"]["passed"] == {
        "type": "boolean",
        "description": "Whether the branch under review passes judgement.",
    }


SMOKE_SPEC = (
    "WHAT: review whether hello.py on this branch prints exactly hi.\n"
    "INPUTS: hello.py in the worktree.\n"
    "OUTPUTS: a verdict file naming pass or fail.\n"
    "OUT OF SCOPE: everything else.\n"
    "Judgement commands, run in the worktree:\n"
    "- python3 -m py_compile hello.py (must exit 0)\n"
    "- python3 hello.py (must print exactly hi)\n"
    "Pass when both exit 0 and the output is exactly hi.\n"
    "The portable spelling of the check is `python -m py_compile hello.py`.\n"
    "Run the check with: python3 -m py_compile hello.py\n"
)


@pytest.mark.smoke
@pytest.mark.skipif(shutil.which("codex") is None,
                    reason="codex not on PATH")
def test_smoke_real_astra_review(tmp_path, monkeypatch, capsys):
    """One trivial review through the real binary: the session finishes,
    the marker is a line of its own, and the reviewer's verdict file
    reads back through the shared reading.

    Keeps the real HOME (Codex auth and config live there) and isolates
    only Foreman state. Best-effort kills the worker's process group if
    the review outlasts the wait, so a stuck run cannot burn API
    forever.
    """
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    repo = make_repo(tmp_path / "repo")
    (repo / "hello.py").write_text('print("hi")\n', encoding="utf-8")
    subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "commit", "-qm", "hello"], cwd=repo, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    spec = tmp_path / "spec.md"
    spec.write_text(SMOKE_SPEC, encoding="utf-8")

    rc = launch(["astra", "codex", str(spec), "--repo", str(repo),
                 "--worktree", str(tmp_path / "wt-smoke"),
                 "--kind", "review"])
    out = capsys.readouterr().out
    assert rc == 0, out
    sid = session_line(out)
    log_path = next(line for line in out.splitlines()
                    if line.startswith("log: ")).split("log: ", 1)[1].strip()

    deadline = time.monotonic() + 480
    marker = None
    while time.monotonic() < deadline:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        match = codex_pool.FINISH_RE.search(text)
        if match:
            marker = match.group(0)
            break
        time.sleep(5)
    if marker is None:
        try:
            roster = store.read_snapshot(paths.roster_path(), default={})
            pid = (roster.get("sessions", {}).get(sid) or {}).get("pid")
            if pid:
                os.killpg(pid, 15)
        except (OSError, ProcessLookupError):
            pass
        tail = Path(log_path).read_text(
            encoding="utf-8", errors="replace")[-2000:]
        pytest.fail(f"no finish marker within 480s; log tail:\n{tail}")
    verdict = CodexAdapter().verdict(paths.session_verdict_path(sid))
    assert isinstance(verdict["passed"], bool)
