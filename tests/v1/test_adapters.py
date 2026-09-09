"""The eight launch fixes: one honest test per fix.

Every test names the break it catches in its docstring. Expected command
lines are hand-derived literals from the fixed shapes, never built with
the adapters' own helpers. Fixture logs use the real vendor field shapes
(Grok's ``--output-format json`` result and ``streaming-json`` NDJSON,
Muse's ``--json`` attribution events) observed on 2026-09-08. No test
starts a vendor process, opens a window, or writes outside tmp dirs;
where the wrapper itself is exercised a stub binary on PATH stands in
for the vendor CLI.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

from foreman import paths, store
from foreman.collector import tick
from foreman.entities import Session
from foreman.pools import LaunchContext, get as get_pool
from foreman.pools import _common as common
from foreman.pools import claude as claude_pool
from foreman.pools import codex as codex_pool
from foreman.pools import grok as grok_pool
from foreman.pools import muse as muse_pool
from foreman.pools import PoolAdapter

ADAPTERS = {
    "muse": muse_pool,
    "grok": grok_pool,
    "claude": claude_pool,
    "codex": codex_pool,
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    monkeypatch.delenv("FOREMAN_SESSION", raising=False)
    return tmp_path


def make_ctx(tmp: Path, pool: str, sid: str = "ses-adapt01",
             kind: str = "implement", effort: str = "high",
             timeout: str = "20m", role: str | None = None,
             window: bool = False) -> LaunchContext:
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
        timeout=timeout,
        effort=effort,
        kind=kind,
        window=window,
    )


def test_working_directory_reaches_every_pool_spawn(env):
    """A worker inheriting the launcher's home directory is one relative
    path away from editing the wrong checkout. Every pool must start its
    unit in the worktree, with no `cd` left in the script."""
    for pool, module in ADAPTERS.items():
        ctx = make_ctx(env, pool, sid=f"ses-wd-{pool}")
        argv = module.outer_argv(ctx)
        assert f"--working-directory={ctx.worktree}" in argv
        assert "cd " not in module.inner_command(ctx)


def test_claude_is_told_its_directory_with_model_and_mode(env):
    """Claude gets `--add-dir` beside the unit directory, and the owner
    ruling still holds: the model and the permission mode stay named."""
    ctx = make_ctx(env, "claude", role="opus")
    argv = claude_pool.claude_argv(ctx)
    assert argv == [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", "claude-opus-5",
        "--dangerously-skip-permissions",
        "--disallowedTools", "mcp__foreman__* mcp__boxes__*",
        "--add-dir", str(ctx.worktree),
    ]


def denies_of(argv: list[str]) -> list[str]:
    """Values of every --deny flag, in order."""
    return [argv[i + 1] for i, part in enumerate(argv) if part == "--deny"]


def test_grok_deny_rules_use_the_documented_grammar(env):
    """Bare `foreman__*` names no ToolPrefix and is silently accepted but
    unenforced, so the owner's deny ruling was a no-op. The exact
    `MCPTool(...)` spellings are the contract; the reviewer Write/Edit/Bash
    bare names are valid and stay."""
    implement = denies_of(grok_pool.grok_argv(make_ctx(env, "grok")))
    assert implement == ["MCPTool(foreman__*)", "MCPTool(boxes__*)"]
    review = denies_of(grok_pool.grok_argv(make_ctx(env, "grok",
                                                    kind="review")))
    assert review == ["MCPTool(foreman__*)", "MCPTool(boxes__*)",
                      "Write", "Edit", "Bash"]


def test_muse_approvals_off_sandbox_on(env):
    """`--yolo` disables approvals AND the OS sandbox; the replacement
    disables approvals only. Any `--yolo` or `--disable-sandbox` in the
    argv reopens the symlink write escape."""
    ctx = make_ctx(env, "muse")
    assert muse_pool.muse_argv(ctx) == [
        "muse", "exec",
        "--model", "muse-spark-1.3-contributor",
        "--reasoning-effort", "high",
        "--approval-mode", "never",
        "--json",
        "--workspace", str(ctx.worktree),
        "--prompt-file", str(ctx.job_path),
    ]
    assert "--yolo" not in muse_pool.muse_argv(ctx)
    assert "--disable-sandbox" not in muse_pool.muse_argv(ctx)


@pytest.mark.parametrize("pool", ["muse", "grok", "claude", "codex"])
def test_no_collect_in_any_spawn(env, pool):
    """A collected unit is gone when it finishes, and the same query then
    reads success defaults — a failed job reads as success. No spawn may
    carry `--collect`."""
    assert "--collect" not in ADAPTERS[pool].outer_argv(make_ctx(env, pool))


@pytest.mark.parametrize("status,failed", [
    ({"LoadState": "loaded", "ActiveState": "failed",
      "Result": "exit-code", "ExecMainStatus": 7}, True),
    ({"LoadState": "loaded", "ActiveState": "inactive",
      "Result": "exit-code", "ExecMainStatus": 3}, True),
    ({"LoadState": "loaded", "ActiveState": "inactive",
      "Result": "success", "ExecMainStatus": 0}, False),
    ({"LoadState": "not-found", "ActiveState": "inactive",
      "Result": "success", "ExecMainStatus": 0}, False),
    (None, False),
])
def test_unit_failure_reading(status, failed):
    """The completion signal decides against the marker: exit-code 7 with
    a failed unit is failed, while a missing unit (or a clean one) leaves
    the marker to decide."""
    assert common.unit_reports_failure(status) is failed


def test_unit_status_parses_systemctl_show():
    """`systemctl show` output becomes typed fields; the seam takes a fake
    run so no test shells out. The sample below is the real shape measured
    on 2026-09-08 for a unit that exited 7."""
    calls = []

    class Proc:
        returncode = 0
        stdout = ("LoadState=loaded\nActiveState=failed\nResult=exit-code\n"
                  "ExecMainStatus=7\nExecMainCode=1\n")

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Proc()

    status = common.unit_status("ses-probe7", run=fake_run)
    assert calls[0][:4] == ["systemctl", "--user", "show",
                            "foreman-ses-probe7"]
    assert status == {"LoadState": "loaded", "ActiveState": "failed",
                      "Result": "exit-code", "ExecMainStatus": 7,
                      "ExecMainCode": 1}
    assert common.unit_reports_failure(status) is True


class ScriptedAdapter(PoolAdapter):
    """Stand-in pool: observe replays one scripted reading per session."""

    name = "fake"
    model = "fake-test-model"
    timeout_default = "5m"
    interactive = False
    script: dict[str, dict] = {}

    def observe(self, session: Session) -> dict:
        return dict(self.script.get(session.id or "", {
            "transcript_mtime": None, "cpu_s": 0.0,
            "finish_present": False, "finish_rc": None}))

    def launch(self, ctx: LaunchContext) -> int:
        raise AssertionError("no launches in this test")

    def command_str(self, ctx: LaunchContext) -> str:
        return "fake-exec"


@pytest.fixture()
def scripted_pool():
    from foreman import pools

    ScriptedAdapter.script = {}
    pools.register("fake", ScriptedAdapter())
    try:
        yield ScriptedAdapter
    finally:
        pools.unregister("fake")


def _dead_session(sid: str, job: str) -> dict:
    return Session(
        id=sid, role="muse", pool="fake", model="fake-test-model",
        front="comp", job=job, pid=99999999, pgid=99999999,
        worktree="", log="", timeout="", state="running",
    ).to_dict()


def test_failed_unit_beats_marker_present(env, scripted_pool, monkeypatch):
    """A job whose unit says it failed is failed even with a marker, while
    a vanished unit falls back to the marker and returns. The collector
    forgets each finished unit only after reading it."""
    from datetime import datetime, timezone

    now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    ScriptedAdapter.script = {
        "ses-unitfail": {"transcript_mtime": 1.0, "cpu_s": 0.0,
                         "finish_present": True, "finish_rc": 0},
        "ses-unitgone": {"transcript_mtime": 1.0, "cpu_s": 0.0,
                         "finish_present": True, "finish_rc": 0},
    }
    store.write_snapshot(paths.roster_path(), {"sessions": {
        "ses-unitfail": _dead_session("ses-unitfail", "job-fail"),
        "ses-unitgone": _dead_session("ses-unitgone", "job-gone"),
    }})
    for job in ("job-fail", "job-gone"):
        store.append_ledger(paths.front_jobs_path("comp"),
                            {"id": job, "state": "running"})
    units = {
        "ses-unitfail": {"LoadState": "loaded", "ActiveState": "failed",
                         "Result": "exit-code", "ExecMainStatus": 7},
        "ses-unitgone": {"LoadState": "not-found",
                         "ActiveState": "inactive",
                         "Result": "success", "ExecMainStatus": 0},
    }
    resets = []

    monkeypatch.setattr(common, "unit_status",
                        lambda sid: units[sid])
    monkeypatch.setattr(common, "reset_failed_unit",
                        lambda sid: resets.append(sid) or True)

    tick(now=now)

    jobs = {line["id"]: line["state"]
            for line in store.read_ledger(paths.front_jobs_path("comp"))}
    assert jobs == {"job-fail": "failed", "job-gone": "returned"}
    assert sorted(resets) == ["ses-unitfail", "ses-unitgone"]


def test_runtime_max_sec_carries_the_timeout(env):
    """systemd enforces the job timeout beside Foreman's own kill: 20m
    becomes RuntimeMaxSec=1200, and an unparsed timeout omits the property
    instead of inventing a number."""
    assert common.timeout_seconds("20m") == 1200
    assert common.timeout_seconds("1h30m") == 5400
    assert common.timeout_seconds("soon") is None
    argv = muse_pool.outer_argv(make_ctx(env, "muse", timeout="20m"))
    assert "--property=RuntimeMaxSec=1200" in argv
    assert "--service-type=exec" in argv
    bare = common.wrap_outer("ses-x", "true", worktree="/wt",
                             timeout="soon")
    assert not [part for part in bare if "RuntimeMaxSec" in part]


def test_exec_shape_still_delivers_pid_and_marker(env, monkeypatch):
    """The measured shape (no setsid): the real wrapper runs against a
    stub vendor and the pid file plus marker still land in the log."""
    ctx = make_ctx(env, "muse")
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    bindir = env / "bin"
    bindir.mkdir()
    stub = bindir / "muse"
    stub.write_text("#!/bin/sh\necho vendor-output\nexit 3\n",
                    encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    inner = muse_pool.inner_command(ctx)
    assert "setsid" not in inner
    assert inner.index("### finished rc=$?") < inner.index("| tee")
    subprocess.run(["bash", "-c", inner], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert int(ctx.pid_path.read_text(encoding="utf-8").strip()) > 0
    lines = ctx.log_path.read_text(encoding="utf-8").splitlines()
    assert "vendor-output" in lines
    assert "### finished rc=3" in lines


GROK_RESULT = """{
  "text": "probe-ok",
  "stopReason": "end_turn",
  "sessionId": "01a08265-6f7d-7db3-bcd5-a706bec1e910",
  "usage": {
    "input_tokens": 17568,
    "cache_read_input_tokens": 128,
    "cache_creation_input_tokens": 0,
    "output_tokens": 40,
    "reasoning_tokens": 33,
    "total_tokens": 17736
  },
  "num_turns": 1,
  "total_cost_usd": 0.0060248
}
"""

GROK_STREAMING = (
    json.dumps({"type": "update", "text": "working"}) + "\n"
    + json.dumps({"type": "update", "text": "still working"}) + "\n"
    + json.dumps({
        "type": "result",
        "text": "probe-ok",
        "stopReason": "end_turn",
        "usage": {
            "input_tokens": 17568,
            "cache_read_input_tokens": 128,
            "cache_creation_input_tokens": 0,
            "output_tokens": 40,
            "reasoning_tokens": 33,
            "total_tokens": 17736,
        },
        "num_turns": 1,
        "total_cost_usd": 0.0060248,
    }) + "\n"
    + json.dumps({"type": "end"}) + "\n"
)

GROK_429 = (
    "API error 429: Subscription quota exhausted. Your usage window resets "
    "at 2026-09-14T00:00:00Z. (rate_limit_error)"
)


def test_grok_argv_streams_json(env):
    """`--output-format json` buffers until exit; streaming-json writes a
    line per update so the log shows life before the job ends."""
    argv = grok_pool.grok_argv(make_ctx(env, "grok"))
    assert argv[argv.index("--output-format") + 1] == "streaming-json"
    assert "json" not in argv


def test_grok_usage_reads_the_json_result(env):
    """Logs already on disk from `--output-format json` must still answer:
    the exact fixture below is the real field shape, plus the finish
    marker the wrapper appends."""
    log = Path(make_ctx(env, "grok").log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(GROK_RESULT + "### finished rc=0\n", encoding="utf-8")
    assert grok_pool.read_usage(log) == {"input_tokens": 17568,
                                         "output_tokens": 40}


def test_grok_usage_reads_the_streaming_result(env):
    """Update lines carry no totals; the final result/usage object does.
    A reader that took the first usage, or that only parsed a single
    JSON object, would miss the run."""
    log = Path(make_ctx(env, "grok").log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(GROK_STREAMING + "### finished rc=0\n", encoding="utf-8")
    assert grok_pool.read_usage(log) == {"input_tokens": 17568,
                                         "output_tokens": 40}


@pytest.mark.parametrize("log_text", [
    "plain prose, no JSON at all\n### finished rc=0\n",
    '{"text": "hi, no usage here"}\n',
    '{"usage": {"input_tokens": "many", "output_tokens": 3}}\n',
    '{"type":"update"}\n{"type":"end"}\n### finished rc=0\n',
])
def test_grok_usage_is_nothing_without_a_counter(env, log_text):
    """A counter that is genuinely absent stays None: an invented number
    is worse than no number."""
    log = env / f"grok-{abs(hash(log_text)) % 10000}.log"
    log.write_text(log_text, encoding="utf-8")
    assert grok_pool.read_usage(log) is None
    assert grok_pool.read_usage(env / "no-such-log") is None


def test_grok_refusal_reads_streaming_and_json(env):
    """A quota refusal in the streaming result, or in the old single
    object, must still put the pool out; a clean stream must not."""
    adapter = grok_pool.GrokAdapter()

    def session_with(name: str, text: str) -> Session:
        log = env / f"{name}.log"
        log.write_text(text, encoding="utf-8")
        return Session(id=name, role="grok", pool="grok",
                       model=grok_pool.MODEL, state="exited", log=str(log))

    blob = json.dumps({"error": GROK_429, "usage": {
        "input_tokens": 1, "output_tokens": 0}}) + "\n"
    found = adapter.refusal(session_with("ses-grok429-json", blob))
    assert found is not None
    assert found["kind"] == "quota"
    assert found["reset"] == "2026-09-14T00:00:00Z"

    stream = (
        json.dumps({"type": "update", "text": "working"}) + "\n"
        + json.dumps({"type": "result", "error": GROK_429}) + "\n"
        + json.dumps({"type": "end"}) + "\n"
        + "### finished rc=1\n"
    )
    found = adapter.refusal(session_with("ses-grok429-ndjson", stream))
    assert found is not None
    assert found["kind"] == "quota"
    assert found["reset"] == "2026-09-14T00:00:00Z"

    clean = adapter.refusal(session_with(
        "ses-grokclean", GROK_STREAMING + "### finished rc=0\n"))
    assert clean is None


def _muse_event(kind: str, record: dict) -> str:
    return json.dumps({"schema_version": 1, "id": "x",
                       "stream": {"kind": "session", "id": "s"},
                       "sequence": 1, "recorded_at": 1,
                       "record_type": "event", "durability": "durable",
                       "causation_id": None,
                       "payload_type": "runtime.session",
                       "payload_schema_version": 1,
                       "payload": {"kind": "run", "run_id": "r",
                                   "event": {"kind": kind,
                                             **record}}})


def test_muse_usage_sums_attribution(env):
    """`--json` must reach the command, and per-goal attribution must come
    back summed: two reported goals of 100/10 and 50/5 total 150/15."""
    ctx = make_ctx(env, "muse")
    assert "--json" in muse_pool.muse_argv(ctx)
    log = Path(ctx.log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        _muse_event("goal_usage_attribution", {"record": {
            "quantity": {"input_tokens": 100, "output_tokens": 10,
                         "cached_tokens": 90, "reasoning_tokens": 4,
                         "reported": True}}}) + "\n"
        + _muse_event("goal_usage_attribution", {"record": {
            "quantity": {"input_tokens": 50, "output_tokens": 5,
                         "cached_tokens": 0, "reasoning_tokens": 0,
                         "reported": True}}}) + "\n"
        + "### finished rc=0\n",
        encoding="utf-8",
    )
    assert muse_pool.read_usage(log) == {"input_tokens": 150,
                                         "output_tokens": 15}


def test_muse_usage_falls_back_to_model_completed(env):
    """Logs with model call usages but no attribution still report: the
    field shape below is the real `model_completed` one."""
    log = env / "muse-model.log"
    log.write_text(
        _muse_event("model_completed", {
            "kind": "model_completed",
            "usage": {"input_tokens": 28407, "output_tokens": 512,
                      "cached_tokens": 0, "reasoning_tokens": 351}}) + "\n",
        encoding="utf-8",
    )
    assert muse_pool.read_usage(log) == {"input_tokens": 28407,
                                         "output_tokens": 512}


@pytest.mark.parametrize("log_text", [
    "plain prose, no JSON at all\n",
    _muse_event("model_completed", {"kind": "model_completed"}) + "\n",
])
def test_muse_usage_is_nothing_without_a_counter(env, log_text):
    """No usable counter means no number, never a zero."""
    log = env / f"muse-{abs(hash(log_text)) % 10000}.log"
    log.write_text(log_text, encoding="utf-8")
    assert muse_pool.read_usage(log) is None
    assert muse_pool.read_usage(env / "no-such-log") is None


def test_codex_verdict_path_is_never_the_last_message_path(env):
    """Pointing `-o` at the verdict file overwrote a real review with one
    sentence of prose. The two paths must differ, and a verdict file
    already claiming the last-message name refuses loudly."""
    ctx = make_ctx(env, "codex")
    last = codex_pool.last_message_path(ctx)
    assert last != ctx.verdict_path
    assert last.name == "last-message.txt"
    argv = codex_pool.codex_argv(ctx)
    assert argv[argv.index("-o") + 1] == str(last)
    assert argv[argv.index("-o") + 1] != str(ctx.verdict_path)
    collision_ctx = make_ctx(env, "codex", sid="ses-collide")
    collision = dataclasses.replace(
        collision_ctx, verdict_path=collision_ctx.verdict_path.parent
        / "last-message.txt")
    with pytest.raises(ValueError):
        codex_pool.last_message_path(collision)
