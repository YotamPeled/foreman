"""`foreman mcp`: role-scoped tools over stdio, and the launch wiring.

Every test drives the real entry points against a fresh ``FOREMAN_STATE``
and ``FOREMAN_CONFIG`` under ``tmp_path``. Expected tool sets are written
out as literals from DESIGN.md section 12 and the gates in the verb
source: a set computed out of the production inventory would pass no
matter what the server listed. The refusal oracle is the CLI itself run
against the same call, not the server's own code path.

The break each test catches is a server that lists a verb its role may
not call (or hides one it may), a refusal reworded away from the CLI's
sentence, a launch that starts a supervisor with no server (or a worker
with one), and a schema that drifts from the argparse flags it claims
to carry.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from foreman import caller as caller_module
from foreman import cli, paths, store
from foreman.caller import SESSION_ENV, SUPERVISOR
from foreman.entities import Session
from foreman.pools import LaunchContext, get as get_pool

ROOT = Path(__file__).resolve().parents[2]
PANEL_BRIEF = ROOT / "briefs" / "panel"

#: What a supervisor session must list: DESIGN section 12's supervisor
#: row as this checkout ships it, one tool per leaf parser. Written out
#: here, not derived: the drift being caught is exactly the server
#: disagreeing with the table.
SUPERVISOR_TOOLS = frozenset({
    "ask", "attach", "check_flake", "checkpoint", "doctor", "evidence", "finding",
    "front_done", "front_import", "front_policy", "front_show", "front_take", "job_cancel", "job_edit", "job_fail", "job_front", "job_land", "job_list", "job_queue", "job_repoint", "job_retry", "job_verify", "kill", "launch", "map_add", "map_import", "map_resolve", "map_show", "measure", "merge_request", "milestone_add", "milestone_list", "milestone_merge", "milestone_split", "node_add", "node_list", "node_prove", "node_revise", "pool_list", "register", "relaunch", "resource_list", "rule",
    "status", "task_add", "task_built", "task_landed", "task_reset", "turn",
    "version", "wait", "wake",
})
#: The foreman role's own row: answers and rules, never front or job verbs.
FOREMAN_TOOLS = frozenset({
    "answer", "attach", "checkpoint", "doctor", "front_allocate",
    "front_land", "front_policy", "front_queue", "front_release", "front_reserve", "front_show", "inbox",
    "kill", "launch", "map_show", "pool_list", "register", "relaunch",
    "resource_list", "rule",
    "status",
    "tell", "turn", "version", "wait", "wake",
})
#: A worker holds no verb, so only the gateless one survives.
WORKER_TOOLS = frozenset({"version"})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FOREMAN_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv("FOREMAN_WINDOW_LAUNCHER", raising=False)
    return tmp_path


def roster_session(session_id: str, role: str, front: str | None = None):
    entry = Session(id=session_id, role=role, pool="muse",
                    model="muse-test", front=front,
                    state="running").to_dict()

    def add(roster):
        if not isinstance(roster, dict):
            roster = {"sessions": {}}
        sessions = roster.setdefault("sessions", {})
        sessions[session_id] = entry
        return roster

    store.update_snapshot(paths.roster_path(), add,
                          default={"sessions": {}})


def request(method: str, params: dict | None = None,
            rpc_id: object = 1) -> dict:
    message: dict = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def tool_names(reply: dict) -> set[str]:
    return {tool["name"] for tool in reply["result"]["tools"]}


# --------------------------------------------------------------------------
# tools/list is already role-scoped when it answers
# --------------------------------------------------------------------------


def test_supervisor_lists_exactly_its_verbs(env, monkeypatch):
    """A supervisor sees its row and nothing else.

    Fails on a server that leaks an owner verb (``front_add`` would let
    a supervisor rewrite the queue) or that drops a shipped one.
    """
    from foreman import mcp as mcp_module

    roster_session("ses-sup", "supervisor", front="panel")
    monkeypatch.setenv(SESSION_ENV, "ses-sup")
    assert set(tool_names(mcp_module.handle(request("tools/list")))) == \
        SUPERVISOR_TOOLS


def test_worker_lists_only_the_open_verbs(env, monkeypatch):
    """A worker holds no verb, so its list is the gateless remainder.

    Fails on a server that hands job or task verbs to a session the
    launcher deliberately started with no server at all.
    """
    from foreman import mcp as mcp_module

    roster_session("ses-wrk", "muse", front="panel")
    monkeypatch.setenv(SESSION_ENV, "ses-wrk")
    assert set(tool_names(mcp_module.handle(request("tools/list")))) == \
        WORKER_TOOLS


def test_foreman_lists_its_own_row(env, monkeypatch):
    """The orchestrator answers questions and writes rulings: no job verbs."""
    from foreman import mcp as mcp_module

    roster_session("ses-for", "foreman")
    monkeypatch.setenv(SESSION_ENV, "ses-for")
    assert set(tool_names(mcp_module.handle(request("tools/list")))) == \
        FOREMAN_TOOLS


def test_owner_without_a_session_lists_everything_but_the_transport(
        env, monkeypatch):
    """No session in the environment is the owner at a terminal: all verbs.

    ``mcp`` itself is never listed: a tool that starts a server on the
    same stdio would hang the caller, so it is transport, not a verb.
    """
    from foreman import mcp as mcp_module

    monkeypatch.delenv(SESSION_ENV, raising=False)
    names = tool_names(mcp_module.handle(request("tools/list")))
    assert "mcp" not in names
    assert SUPERVISOR_TOOLS | FOREMAN_TOOLS | {"front_add", "cap",
                                              "collector"} <= names
    # 34 before the doctor/hooks/migrations job: doctor, freeze, thaw,
    # hook_install, hook_list, migrate; plus task add and front done from
    # the launches job, and kill, the verb the design gave the owner and
    # this runtime had never shipped; plus tell and wake from the wake
    # events job, the clock the headless turn loop reads; plus attach
    # from the headless status job, the window on a session's turn log;
    # plus turn from the collector-carries job, the tick's hands; plus
    # wait from the ledger-waiter job, the collector's verdict; plus
    # front show from the v5 inputs job; plus node add, revise, list
    # and prove from the tree door; plus front policy from the landing
    # policy job; plus job land from the landing script; plus front land
    # from the front-landing job.
    # policy job; plus job land from the landing script; plus job retry
    # from the review-states job; plus start from the foreman-start job
    # (an owner verb: every session role is refused it); plus clockwork
    # from the clockwork job (owner-only: the collector's own table).
    # (an owner verb: every session role is refused it); plus front queue
    # from the front-queue job.
    # Counted, not derived, so a verb added without intent fails here.
    assert len(names) == 78


def test_unknown_session_lists_only_the_open_verbs(env, monkeypatch):
    """An id the roster never minted is no role at all, not the owner."""
    from foreman import mcp as mcp_module

    roster_session("ses-sup", "supervisor", front="panel")
    monkeypatch.setenv(SESSION_ENV, "ses-nobody")
    assert set(tool_names(mcp_module.handle(request("tools/list")))) == \
        WORKER_TOOLS


def test_every_role_sees_a_different_list(env, monkeypatch):
    """The supervisor/worker split the spec demands, as sets, not vibes."""
    from foreman import mcp as mcp_module

    roster_session("ses-sup", "supervisor", front="panel")
    roster_session("ses-wrk", "muse", front="panel")
    monkeypatch.setenv(SESSION_ENV, "ses-sup")
    supervisor = set(tool_names(mcp_module.handle(request("tools/list"))))
    monkeypatch.setenv(SESSION_ENV, "ses-wrk")
    worker = set(tool_names(mcp_module.handle(request("tools/list"))))
    assert worker < supervisor


# --------------------------------------------------------------------------
# tools/call goes through the handler main would call
# --------------------------------------------------------------------------


def test_refusal_over_mcp_is_byte_identical_to_the_cli(
        env, monkeypatch, capsys):
    """The same call refused the same way over both doors.

    Fails on a server that rewords a refusal instead of replaying the
    handler: the CLI's sentence is the contract, not a suggestion.
    """
    from foreman import mcp as mcp_module

    roster_session("ses-sup", "supervisor", front="panel")
    monkeypatch.setenv(SESSION_ENV, "ses-sup")
    capsys.readouterr()
    assert cli.main(["task", "built", "no-such-task"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""

    text, failed = mcp_module.call_tool("task_built",
                                        {"task": "no-such-task"})
    assert failed is True
    assert text == captured.err
    assert text == "foreman: refused: unknown task 'no-such-task'\n"


def test_role_refusal_names_every_violation_like_the_cli(
        env, monkeypatch, capsys):
    """A worker reaching for a task verb gets every violation at once."""
    from foreman import mcp as mcp_module

    roster_session("ses-wrk", "muse", front="panel")
    monkeypatch.setenv(SESSION_ENV, "ses-wrk")
    capsys.readouterr()
    assert cli.main(["task", "built", "no-such-task"]) == 1
    cli_err = capsys.readouterr().err

    text, failed = mcp_module.call_tool("task_built",
                                        {"task": "no-such-task"})
    assert failed is True
    assert text == cli_err
    assert "role 'muse' may not call 'task built'" in text


def test_successful_call_prints_what_the_cli_prints(
        env, monkeypatch, capsys):
    """A call that passes prints the handler's own lines, not a summary."""
    from foreman import mcp as mcp_module

    roster_session("ses-sup", "supervisor", front="panel")
    monkeypatch.setenv(SESSION_ENV, "ses-sup")
    capsys.readouterr()
    assert cli.main(["checkpoint", "--doing", "splitting the scope",
                     "--next", "dispatching unit one"]) == 0
    cli_out = capsys.readouterr().out

    text, failed = mcp_module.call_tool(
        "checkpoint", {"doing": "splitting the scope",
                       "next": "dispatching unit two"})
    assert failed is False
    assert text == "checkpoint ses-sup\n"
    assert cli_out == "checkpoint ses-sup\n"


def test_unknown_tool_and_bad_arguments_are_errors(env, monkeypatch):
    """Misspelt tools and mistyped arguments fail loudly, never silently."""
    from foreman import mcp as mcp_module

    roster_session("ses-sup", "supervisor", front="panel")
    monkeypatch.setenv(SESSION_ENV, "ses-sup")

    text, failed = mcp_module.call_tool("job_promote", {"job": "job-1"})
    assert failed is True
    assert "unknown tool 'job_promote'" in text

    text, failed = mcp_module.call_tool("task_built", {"task": "t"})
    assert failed is True
    assert text == "foreman: refused: unknown task 't'\n"

    text, failed = mcp_module.call_tool("task_built",
                                        {"task": "t", "bogus": "x"})
    assert failed is True
    assert "unknown argument 'bogus'" in text

    text, failed = mcp_module.call_tool("job_verify",
                                        {"job": "j", "confirmed": "yes"})
    assert failed is True
    assert "argument 'confirmed' must be a boolean" in text


# --------------------------------------------------------------------------
# The schemas come from the CLI's own argparse definitions
# --------------------------------------------------------------------------


def test_schemas_carry_the_flags_the_cli_registers(env):
    """Spot checks with hand-derived shapes, not with the code's helpers."""
    from foreman import mcp as mcp_module

    by_name = {tool["name"]: tool for tool in mcp_module.list_tools()}

    verify = by_name["job_verify"]["inputSchema"]
    assert verify["type"] == "object"
    assert verify["required"] == ["job"]
    assert verify["properties"]["job"] == {"type": "string",
                                           "description": "job id"}
    assert verify["properties"]["confirmed"] == {"type": "boolean",
                                                "description": (
                                                    "the supervisor re-ran "
                                                    "the verification")}

    point = by_name["checkpoint"]["inputSchema"]
    assert point.get("required", []) == []
    assert point["properties"]["held"]["type"] == "array"

    ask = by_name["ask"]["inputSchema"]
    assert ask["properties"]["question"]["type"] == "array"

    rule = by_name["rule"]["inputSchema"]
    assert rule["required"] == ["scope"]

    launch = by_name["launch"]["inputSchema"]
    assert launch["properties"]["kind"]["enum"] == [
        "implement", "review", "merge", "research", "verify"]
    assert launch["properties"]["kind"]["default"] == "implement"

    for tool in by_name.values():
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert set(schema.get("required", [])) <= \
            set(schema["properties"])


def _trial_probe(args) -> int:
    """A verb registered inside one test only, gated to the supervisor."""
    me, violations = caller_module.resolve("trialprobe")
    caller_module.check_role(me, "trialprobe", SUPERVISOR,
                             violations=violations)
    if violations:
        from foreman.caller import Refusal as _Refusal
        return _Refusal(violations).report()
    print(f"probed {args.level}")
    return 0


def _add_trial_arguments(sub) -> None:
    sub.add_argument("--level", default=None,
                     help="how hard to probe (required)")


_trial_probe.add_arguments = _add_trial_arguments  # type: ignore[attr-defined]


def test_a_verb_gaining_a_flag_gains_it_in_both_places(env, monkeypatch):
    """The inventory is read, not kept: a new verb arrives with its flags.

    Fails on a server with a hand-written tool list, which would keep
    serving yesterday's verbs after the CLI learned a new one.
    """
    from foreman import mcp as mcp_module

    roster_session("ses-sup", "supervisor", front="panel")
    monkeypatch.setenv(SESSION_ENV, "ses-sup")
    monkeypatch.setitem(cli.SUBCOMMANDS, "trialprobe",
                        (_trial_probe, {"help": "A verb added for one test."}))

    names = tool_names(mcp_module.handle(request("tools/list")))
    assert "trialprobe" in names
    by_name = {tool["name"]: tool for tool in mcp_module.list_tools()}
    assert by_name["trialprobe"]["inputSchema"]["properties"]["level"] == \
        {"type": "string", "description": "how hard to probe (required)"}
    text, failed = mcp_module.call_tool("trialprobe", {"level": "hard"})
    assert (text, failed) == ("probed hard\n", False)


# --------------------------------------------------------------------------
# The wire itself, over a real pipe
# --------------------------------------------------------------------------


def test_server_answers_over_a_real_pipe_as_each_role(env):
    """The loop speaks newline-delimited JSON-RPC on a real subprocess.

    In-process calls alone could hide a framing bug (a missing newline,
    a response on stderr); this test starts ``foreman mcp`` for real, as
    a supervisor and as a worker, and reads the bytes back.
    """
    roster_session("ses-sup", "supervisor", front="panel")
    roster_session("ses-wrk", "muse", front="panel")

    def run_as(session_id: str) -> list[dict]:
        transport = [
            request("initialize", {}),
            request("tools/list", {}, rpc_id=2),
            request("tools/call", {"name": "version", "arguments": {}},
                    rpc_id=3),
        ]
        child_env = dict(os.environ,
                         FOREMAN_STATE=str(env / "state"),
                         FOREMAN_CONFIG=str(env / "config"),
                         FOREMAN_SESSION=session_id,
                         PYTHONPATH=str(ROOT / "src"))
        proc = subprocess.run(
            [sys.executable, "-m", "foreman", "mcp"],
            input="".join(json.dumps(line) + "\n" for line in transport),
            capture_output=True, text=True, timeout=60, env=child_env,
            cwd=str(ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr == ""
        return [json.loads(line) for line in proc.stdout.splitlines()]

    supervisor = run_as("ses-sup")
    assert supervisor[0]["result"]["protocolVersion"] == "2024-11-05"
    assert supervisor[0]["result"]["serverInfo"]["name"] == "foreman"
    assert {tool["name"] for tool in supervisor[1]["result"]["tools"]} == \
        SUPERVISOR_TOOLS
    assert supervisor[2]["result"]["isError"] is False

    worker = run_as("ses-wrk")
    assert {tool["name"] for tool in worker[1]["result"]["tools"]} == \
        WORKER_TOOLS


def test_server_survives_noise_on_the_wire(env, monkeypatch):
    """Blank lines are skipped, garbage gets a parse error, the loop lives."""
    from foreman import mcp as mcp_module

    roster_session("ses-sup", "supervisor", front="panel")
    monkeypatch.setenv(SESSION_ENV, "ses-sup")
    out = io.StringIO()
    assert mcp_module.serve(io.StringIO(
        "\n"
        "{not json\n"
        '{"jsonrpc": "2.0", "id": 7, "method": "ping"}\n'
        '{"jsonrpc": "2.0", "id": 8, "method": "no_such_method"}\n'
    ), out) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert replies[2]["error"]["code"] == -32601


# --------------------------------------------------------------------------
# Launch wiring: supervisors get a server, workers get none
# --------------------------------------------------------------------------


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "test")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("add", "seed.txt")
    run("commit", "-qm", "seed")
    return path


def line(out: str, prefix: str) -> str:
    for row in out.splitlines():
        if row.startswith(prefix):
            return row.split(prefix, 1)[1].strip()
    raise AssertionError(f"no {prefix!r} line in output:\n{out}")


def make_ctx(tmp: Path, pool: str, sid: str = "ses-mcp",
             kind: str = "implement") -> LaunchContext:
    session = Session(id=sid, role="muse", pool=pool,
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
        kind=kind,
    )


def test_no_worker_launch_carries_a_server(env):
    """Every pool's worker command denies the swarm's tools by name and
    names no MCP config: a worker with a server is the failure the deny
    exists to prevent. Fails on any adapter that starts passing one."""
    from foreman.pools import claude as claude_pool
    from foreman.pools import codex as codex_pool
    from foreman.pools import grok as grok_pool
    from foreman.pools import muse as muse_pool

    modules = {"muse": muse_pool, "grok": grok_pool, "claude": claude_pool,
               "codex": codex_pool}
    for pool, module in modules.items():
        ctx = make_ctx(env, pool, sid=f"ses-{pool}")
        command = (module.outer_argv(ctx), module.inner_command(ctx))
        text = " ".join(" ".join(part) if isinstance(part, list) else part
                        for part in command for part in
                        ([part] if isinstance(part, str) else part))
        assert "--mcp-config" not in text, pool
        assert "--strict-mcp-config" not in text, pool
        assert "mcp.json" not in text, pool
        assert "mcpServers" not in text, pool

    claude_text = claude_pool.inner_command(make_ctx(env, "claude"))
    assert "--disallowedTools" in claude_text
    assert "mcp__foreman__*" in claude_text

    grok_text = grok_pool.inner_command(make_ctx(env, "grok"))
    assert "MCPTool(foreman__*)" in grok_text


def test_supervisor_dry_run_prints_the_mcp_wiring(env, capsys):
    """``launch supervisor --dry-run`` shows the server it would register.

    Fails on a summon that starts Claude with no ``--mcp-config`` (then
    the supervisor has no tools) or that writes no ``mcp.json`` for the
    flags to point at (then Claude fails to start).
    """
    from foreman import mcp as mcp_module

    repo = make_repo(env / "repo")
    assert cli.main(["front", "add", str(PANEL_BRIEF)]) == 0
    capsys.readouterr()
    assert cli.main(["launch", "supervisor", "panel", "--workspace", "6",
                     "--repo", str(repo), "--dry-run"]) == 0
    out = capsys.readouterr().out
    sid = line(out, "session: ")
    config_path = Path(line(out, "mcp config: "))
    assert config_path == mcp_module.mcp_config_path(sid)
    assert f"--mcp-config {config_path} --strict-mcp-config" in out

    config = json.loads(config_path.read_text(encoding="utf-8"))
    # rul-ym3xjam: the config names the absolute interpreter and package
    # of the checkout the launch came from, never a bare `foreman` off
    # PATH — a session summoned from a branch checkout runs that branch's
    # verbs over MCP too.
    server = config["mcpServers"]["foreman"]
    assert server["type"] == "stdio"
    assert server["command"] == str(Path(sys.executable).resolve())
    assert server["command"] != "foreman"
    assert server["args"] == ["-m", "foreman", "mcp"]
    assert server["env"]["FOREMAN_SESSION"] == sid
    expected_src = Path(__file__).resolve().parents[2] / "src"
    assert (expected_src / "foreman" / "__init__.py").exists()
    assert server["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(
        expected_src)


def test_relaunch_dry_run_rewrites_the_mcp_wiring(env, capsys):
    """A relaunched supervisor answers to its own session, config included.

    The relaunch keeps the Foreman session id and starts a fresh vendor
    conversation, so the wiring must name that same session — and the
    vendor line must carry the role prompt, not a `--resume` the session
    could never be handed a prompt through.
    """
    from foreman import mcp as mcp_module

    repo = make_repo(env / "repo")
    assert cli.main(["front", "add", str(PANEL_BRIEF)]) == 0
    capsys.readouterr()
    assert cli.main(["register", "--role", "supervisor", "--front", "panel",
                     "--pid", str(os.getpid()),
                     "--session", "a-vendor-uuid"]) == 0
    old = line(capsys.readouterr().out, "session: ")
    assert cli.main(["relaunch", old, "--workspace", "6",
                     "--repo", str(repo), "--dry-run"]) == 0
    out = capsys.readouterr().out
    sid = line(out, "session: ")
    assert sid == old
    assert "--resume" not in out
    # A fresh conversation, so a vendor id that is not the one registered.
    assert line(out, "vendor session: ") != "a-vendor-uuid"
    config_path = Path(line(out, "mcp config: "))
    assert f"--mcp-config {config_path} --strict-mcp-config" in out
    config = json.loads(config_path.read_text(encoding="utf-8"))
    env = config["mcpServers"]["foreman"]["env"]
    assert env["FOREMAN_SESSION"] == sid
    expected_src = Path(__file__).resolve().parents[2] / "src"
    assert (expected_src / "foreman" / "__init__.py").exists()
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(expected_src)


def test_a_gate_written_with_a_qualified_role_is_still_read(env):
    """`caller.SUPERVISOR` and bare `SUPERVISOR` are the same gate.

    The gate reader took the role off an `ast.Name` only, so a verb whose
    module imports the constant qualified — `caller.check_role(me, verb,
    caller.SUPERVISOR)` — was read as having no roles at all and listed for
    nobody. `front take` was missing from every supervisor's tools while
    the CLI happily ran it.
    """
    from foreman import launch as launch_module

    gates, _required = launch_module._gate_tables()
    assert "supervisor" in gates["front take"]


def test_a_worker_is_offered_no_verb_however_the_gate_is_written(env):
    """A worker holds one open verb, whatever shape a module's gate takes.

    Two ways a gate went unread, both found by merging real work into this
    branch. A module that puts its gate in a helper — `_check_desk(me,
    verb, violations)` — names the verb by a parameter, so the gate read as
    absent and `merge land`, which rebases and pushes, was offered to every
    worker. And the reader looks its modules up in `sys.modules`, so a
    module nothing had imported yet had no gates at all: which verbs were
    open depended on what had been imported first.
    """
    from foreman import mcp as mcp_module

    assert {tool["name"] for tool in mcp_module.tools_for("muse")} == \
        WORKER_TOOLS
    desk = {tool["name"] for tool in mcp_module.tools_for("merge-desk")}
    assert {"merge_take", "merge_land", "merge_fail"} <= desk
    assert "task_built" not in desk


def test_the_gate_reader_imports_the_modules_it_reads(env):
    """Reading gates out of sys.modules alone made the answer depend on
    import order. Every verb module is imported first, so it does not.

    A fresh interpreter is the only honest way to ask: unloading modules
    inside this one leaves two copies of every class behind and breaks the
    tests that run after it.
    """
    import subprocess
    import sys

    code = (
        "from foreman import launch\n"
        "gates, _ = launch._gate_tables()\n"
        "print(sorted(gates['merge land']), sorted(gates['measure']))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "['merge-desk'] ['supervisor']"
