"""`foreman mcp`: every verb as a tool, scoped by the caller's session.

A supervisor that can only run verbs through a shell keeps its whole
loop in prose; a tool call keeps the arguments typed. This server speaks
JSON-RPC 2.0 over stdio (``initialize``, ``tools/list``, ``tools/call``,
no SDK, standard library only) so a Claude session summoned with
``--mcp-config`` gets exactly the verbs its role may call.

The inventory is never written by hand. One tool per leaf parser the CLI
registers, with its ``inputSchema`` read off that parser's own argparse
actions, so a verb gaining a flag gains it in both places. Who may call
it is read off the same gates the role prompt reads (``check_role`` and
``check_front_supervisor`` in the verb source): the owner gets every
tool, an unknown session only the open ones, everyone else exactly its
row. ``mcp`` itself is transport, not a verb, and is never listed: a
tool that starts a server on the same stdio would hang the caller.

A call runs the same handler ``main`` would run, with the arguments
replayed as a command line through the same parser, so a refusal is the
CLI's own sentence rather than a new one. Nothing here prints to stdout
but responses; everything else goes to stderr.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from . import caller, cli, paths
from .caller import SESSION_ENV
from .cli import subcommand

#: Name under ``mcpServers`` and in ``mcp__<server>__<tool>``.
SERVER_NAME = "foreman"
#: Beside the session's pid file and role prompt, never in a worktree.
MCP_CONFIG_FILENAME = "mcp.json"
#: The protocol version this server answers with.
PROTOCOL_VERSION = "2024-11-05"


def _ensure_verbs() -> None:
    """Import every verb module, the way the entry point does.

    Both the parser walk and the gate table only know about modules
    already imported; without this a server started fresh (``foreman
    mcp`` imports nothing else first) would list one tool.
    """
    from . import collector as _collector  # noqa: F401
    from . import config as _config  # noqa: F401
    from . import doctor as _doctor  # noqa: F401
    from . import fronts as _fronts  # noqa: F401
    from . import headless as _headless  # noqa: F401
    from . import hooks as _hooks  # noqa: F401
    from . import launch as _launch  # noqa: F401
    from . import migrate as _migrate  # noqa: F401
    from . import progress as _progress  # noqa: F401
    from . import status as _status  # noqa: F401
    from . import verbs as _verbs  # noqa: F401
    from . import wake as _wake  # noqa: F401


def mcp_config_path(session_id: str) -> Path:
    """Where a summoned session's Claude MCP file lives."""
    return paths.session_dir(session_id) / MCP_CONFIG_FILENAME


def checkout_src() -> Path | None:
    """This checkout's ``src`` directory, when this module runs from one.

    A session summoned from a branch checkout must run that branch's
    verbs over MCP, not whatever ``foreman`` resolves to off PATH — so
    the config below names this checkout explicitly. An installed wheel
    has no checkout and answers None, and the config then names only the
    absolute interpreter.
    """
    src = Path(__file__).resolve().parent.parent
    if src.name == "src" and (src / "foreman" / "__init__.py").exists():
        return src
    return None


def mcp_server_env(session_id: str) -> dict[str, str]:
    """Environment the MCP server runs with: this session, this checkout.

    ``PYTHONPATH`` puts the checkout this launch came from first, so a
    session summoned from a branch checkout runs that branch's verbs.
    The launcher's own ``PYTHONPATH`` is kept behind it, never replaced:
    replacing it would drop whatever the owner's environment needed.
    """
    env = {SESSION_ENV: session_id}
    src = checkout_src()
    if src is not None:
        inherited = os.environ.get("PYTHONPATH")
        env["PYTHONPATH"] = str(src) + (
            os.pathsep + inherited if inherited else "")
    return env


def mcp_config_text(session_id: str) -> str:
    """The file content: this server over stdio, as this session.

    Names the absolute interpreter running this launch
    (``sys.executable -m foreman``), never a bare ``foreman`` off PATH:
    a session summoned from a branch checkout runs that branch's verbs
    over MCP too, via ``PYTHONPATH`` (see :func:`mcp_server_env`).
    """
    return json.dumps(
        {"mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": str(Path(sys.executable).resolve()),
                "args": ["-m", "foreman", "mcp"],
                "env": mcp_server_env(session_id),
            },
        }},
        indent=2,
    ) + "\n"


def write_mcp_config(session_id: str) -> Path:
    """Write the session's MCP file. Same helper for every summon."""
    path = mcp_config_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(mcp_config_text(session_id), encoding="utf-8")
    return path


def mcp_config_flags(session_id: str) -> list[str]:
    """Claude flags pinning the session to exactly this server's tools."""
    return ["--mcp-config", str(mcp_config_path(session_id)),
            "--strict-mcp-config"]


# --------------------------------------------------------------------------
# The inventory: one tool per leaf parser, roles off the verb gates.
# --------------------------------------------------------------------------


def _roles_for_forms(gates: dict[str, set[str]],
                     forms: list[str]) -> set[str] | None:
    """Roles allowed to call any of ``forms``; None means open to all."""
    if not forms:
        return None
    allowed: set[str] = set()
    for verb in forms:
        allowed.update(gates.get(verb, set()))
    return allowed


def _prop_name(action: argparse.Action) -> str:
    """The tool argument naming one argparse action.

    An option is named by its longest flag (``--dry-run`` is ``dry_run``,
    ``--class`` is ``class``); a positional keeps its dest.
    """
    if action.option_strings:
        return max(action.option_strings, key=len).lstrip("-").replace("-", "_")
    return action.dest


def _scalar_schema(action: argparse.Action) -> dict:
    """The JSON type for one scalar value this action stores."""
    schema: dict = {}
    action_type = getattr(action, "type", None)
    if action_type is int:
        schema["type"] = "integer"
    elif action_type is float:
        schema["type"] = "number"
    else:
        schema["type"] = "string"
    if action.choices:
        schema["enum"] = list(action.choices)
    if action.help:
        schema["description"] = action.help
    return schema


def _action_schema(action: argparse.Action) -> dict:
    """The ``inputSchema`` property for one argparse action."""
    if isinstance(action, argparse._StoreTrueAction):
        schema: dict = {"type": "boolean"}
    elif isinstance(action, argparse._StoreFalseAction):
        schema = {"type": "boolean"}
    elif isinstance(action, argparse._CountAction):
        schema = {"type": "integer"}
    elif isinstance(action, argparse._AppendAction):
        schema = {"type": "array", "items": _scalar_schema(action)}
    elif action.nargs in ("*", "+"):
        schema = {"type": "array", "items": _scalar_schema(action)}
    else:
        schema = _scalar_schema(action)
    if (action.default is not None
            and not isinstance(action, (argparse._StoreTrueAction,
                                        argparse._StoreFalseAction))):
        schema["default"] = action.default
    if action.help and "description" not in schema:
        schema["description"] = action.help
    return schema


def _is_required(action: argparse.Action) -> bool:
    """Whether omitting the argument fails the parse."""
    if action.option_strings:
        return bool(action.required)
    return action.nargs is None


def _schema_for(parser: argparse.ArgumentParser) -> dict:
    """An MCP ``inputSchema`` read off one leaf parser's own actions."""
    properties: dict[str, dict] = {}
    required: list[str] = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction,
                               argparse._SubParsersAction)):
            continue
        name = _prop_name(action)
        properties[name] = _action_schema(action)
        if _is_required(action):
            required.append(name)
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _describe(path: str, help_text: str) -> str:
    if help_text:
        return f"foreman {path}: {help_text}"
    return f"Run `foreman {path}`."


def list_tools() -> list[dict]:
    """Every tool: name, description, schema, and who may call it.

    ``roles`` is None for a verb with no gate (open to every caller) and
    otherwise the union of the gate's roles over the tool's forms. The
    entry point filters by the connected session; see :func:`tools_for`.
    """
    from . import launch as launch_module

    _ensure_verbs()
    gates, _required = launch_module._gate_tables()
    tools: list[dict] = []
    for path, parser, help_text in launch_module.registered_verbs():
        if path == "mcp":
            continue
        sub = launch_module._subparsers(parser)
        if sub is None:
            forms = [verb for verb in gates
                     if verb == path or verb.startswith(path + " ")]
            tools.append({
                "name": path.replace(" ", "_").replace("-", "_"),
                "path": path.split(),
                "subverb": None,
                "parser": parser,
                "description": _describe(path, help_text),
                "inputSchema": _schema_for(parser),
                "roles": _roles_for_forms(gates, forms),
            })
        else:
            helps = {choice.dest: (choice.help or "")
                     for choice in sub._choices_actions}
            for subverb, subparser in sub.choices.items():
                form = f"{path} {subverb}"
                forms = [verb for verb in gates
                         if verb == form or verb.startswith(form + " ")]
                roles = (None if form not in gates and not forms
                         else _roles_for_forms(gates, [form]))
                tools.append({
                    "name": form.replace(" ", "_").replace("-", "_"),
                    "path": path.split(),
                    "subverb": subverb,
                    "parser": subparser,
                    "description": _describe(form, helps.get(subverb, "")),
                    "inputSchema": _schema_for(subparser),
                    "roles": roles,
                })
    return tools


def tools_for(role: str | None) -> list[dict]:
    """The tools ``role`` may call.

    None is the owner: every tool. Any other string filters by the
    gates, so an unknown session (or a worker, which holds no verb)
    keeps only the open ones.
    """
    if role is None:
        return list_tools()
    return [tool for tool in list_tools()
            if tool["roles"] is None or role in tool["roles"]]


def resolve_role() -> tuple[str | None, bool]:
    """The connected session's role; (None, _) is the owner.

    An unknown session id reports ("", False): not the owner, with no
    role to judge, so it lists only the open tools and every gated call
    refuses with the CLI's own sentence. The flag is carried for callers
    that need to tell unknown apart from worker.
    """
    me, _violations = caller.resolve("mcp")
    if me is None:
        if os.environ.get(SESSION_ENV):
            return "", False
        return None, True
    if me.role == caller.OWNER:
        return None, True
    return me.role, True


# --------------------------------------------------------------------------
# Calls: arguments back through the CLI's own parser and handler.
# --------------------------------------------------------------------------


def _find_tool(name: str) -> dict | None:
    for tool in list_tools():
        if tool["name"] == name:
            return tool
    return None


def _check_value(name: str, schema: dict, value: object) -> str | None:
    """Why ``value`` does not fit one property's schema, or None."""
    kind = schema.get("type")
    if kind == "boolean" and not isinstance(value, bool):
        return f"argument '{name}' must be a boolean"
    if kind == "array" and not isinstance(value, list):
        return f"argument '{name}' must be an array"
    if kind == "integer" and not (isinstance(value, int)
                                 and not isinstance(value, bool)):
        return f"argument '{name}' must be an integer"
    if kind not in ("boolean", "array", "integer") and isinstance(value, list):
        return f"argument '{name}' must be a single value, not an array"
    return None


def tool_argv(tool: dict, arguments: dict) -> tuple[list[str], str | None]:
    """Replay tool arguments as the command line ``main`` would parse."""
    parser = tool["parser"]
    actions = [action for action in parser._actions
               if not isinstance(action, (argparse._HelpAction,
                                          argparse._SubParsersAction))]
    by_prop = {_prop_name(action): action for action in actions}
    for name in arguments:
        if name not in by_prop:
            return [], f"unknown argument '{name}'"
    properties = tool["inputSchema"].get("properties", {})
    argv: list[str] = [*tool["path"]]
    if tool["subverb"] is not None:
        argv.append(tool["subverb"])
    positional: list[str] = []
    optional: list[str] = []
    for action in actions:
        name = _prop_name(action)
        if name not in arguments:
            continue
        value = arguments[name]
        problem = _check_value(name, properties.get(name, {}), value)
        if problem is not None:
            return [], problem
        if value is None:
            continue
        if isinstance(action, argparse._StoreTrueAction):
            if value:
                optional.append(action.option_strings[-1])
        elif isinstance(action, argparse._StoreFalseAction):
            if not value:
                optional.append(action.option_strings[-1])
        elif isinstance(action, argparse._CountAction):
            optional.extend([action.option_strings[-1]] * int(value))
        elif isinstance(action, argparse._AppendAction):
            items = value if isinstance(value, list) else [value]
            for item in items:
                optional += [action.option_strings[-1], str(item)]
        elif action.option_strings:
            optional += [action.option_strings[-1], str(value)]
        elif action.nargs in ("*", "+"):
            items = value if isinstance(value, list) else [value]
            positional.extend(str(item) for item in items)
        else:
            positional.append(str(value))
    return argv + positional + optional, None


def call_tool(name: str, arguments: dict) -> tuple[str, bool]:
    """Run one tool; the handler's own output, and whether it refused.

    Both streams are captured together so a refusal reads exactly as the
    CLI prints it, and so is a success: whatever ``main`` would have
    printed, in the order it printed it.
    """
    _ensure_verbs()
    tool = _find_tool(name)
    if tool is None:
        return f"unknown tool '{name}'", True
    if not isinstance(arguments, dict):
        return "argument 'arguments' must be an object", True
    argv, problem = tool_argv(tool, arguments)
    buffer = io.StringIO()
    if problem is not None:
        return problem, True
    with redirect_stdout(buffer), redirect_stderr(buffer):
        try:
            parsed = cli.build_parser().parse_args(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
            return buffer.getvalue(), code != 0
        try:
            rc = parsed._handler(parsed)
        except Exception as exc:  # noqa: BLE001 - a tool never traces
            return f"foreman: error: {type(exc).__name__}: {exc}", True
    code = rc if isinstance(rc, int) else 0
    return buffer.getvalue(), code != 0


# --------------------------------------------------------------------------
# The wire: newline-delimited JSON-RPC 2.0, responses on stdout only.
# --------------------------------------------------------------------------


def _error(error_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": error_id, "error": {
        "code": code, "message": message}}


def _ok(error_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": error_id, "result": result}


def handle(message: object) -> dict | None:
    """One JSON-RPC message; None means notify and stay silent."""
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request: not an object")
    method = message.get("method")
    error_id = message.get("id")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(error_id, -32602, "invalid params: not an object")
    if not isinstance(method, str):
        return _error(error_id, -32600, "invalid request: no method")
    if error_id is None:
        if method in ("initialize", "tools/list", "tools/call", "ping"):
            return None
        if method.startswith("notifications/"):
            return None
        return None
    if method == "initialize":
        return _ok(error_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME,
                           "version": _package_version()},
        })
    if method == "ping":
        return _ok(error_id, {})
    if method == "tools/list":
        role, _known = resolve_role()
        listed = list_tools() if role is None else tools_for(role)
        return _ok(error_id, {"tools": [
            {"name": tool["name"],
             "description": tool["description"],
             "inputSchema": tool["inputSchema"]}
            for tool in listed
        ]})
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(tool_name, str) or not tool_name:
            return _error(error_id, -32602,
                           "invalid params: 'name' is required")
        if arguments is None:
            arguments = {}
        text, failed = call_tool(tool_name, arguments)
        return _ok(error_id, {
            "content": [{"type": "text", "text": text}],
            "isError": failed,
        })
    return _error(error_id, -32601, f"unknown method '{method}'")


def _package_version() -> str:
    try:
        from . import __version__ as version
    except ImportError:
        return "0.0.0"
    return version


def serve(stdin=None, stdout=None) -> int:
    """Read requests off stdin, write responses to stdout, logs to stderr."""
    reading = stdin if stdin is not None else sys.stdin
    writing = stdout if stdout is not None else sys.stdout
    while True:
        line = reading.readline()
        if not line:
            return 0
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _error(None, -32700, f"parse error: {exc}")
        else:
            try:
                response = handle(message)
            except Exception as exc:  # noqa: BLE001 - the wire never traces
                print(f"foreman mcp: error: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                error_id = (message.get("id")
                            if isinstance(message, dict) else None)
                response = _error(error_id, -32603, f"internal error: {exc}")
        if response is None:
            continue
        try:
            writing.write(json.dumps(response) + "\n")
            writing.flush()
        except BrokenPipeError:
            return 0


@subcommand("mcp", help="Serve the verbs as role-scoped MCP tools over stdio.")
def cmd_mcp(args: argparse.Namespace) -> int:
    return serve()
