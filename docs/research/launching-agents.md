# Launching headless agent workers: better than hand-written shell shapes?

Researched 2026-09-08. Every claim below comes from a fetched vendor page, a shipped `--help`/doc of
the installed CLI, or a command run here; anything else is marked UNVERIFIED. URLs at the end.

## 1. What each vendor's official interface gives

| Capability | Claude Code | OpenAI Codex | xAI Grok | Meta Muse |
|---|---|---|---|---|
| Official SDK | Agent SDK, Python + TS | Codex SDK, Python + TS (drives app-server over JSON-RPC) | none first-party; ACP client libs instead | TS SDK only (`0.1.1`, preview); no Python |
| Structured completion | last `stream-json` line is `result` (`subtype`, `is_error`, `num_turns`); exit 0/non-zero | `turn.completed` / `turn.failed` in `exec --json`; app-server `turn/completed` | `--output-format json` prints one object with `stopReason`; `streaming-json` ends with `{"type":"end"}` | `run.terminal.completed` record in `exec --json`; MSP `turn/completed` |
| Documented exit codes | 0 / non-zero; SIGTERM → 143 | non-zero on failure (detail UNVERIFIED) | 0 / 1 / 130 SIGINT / 143 SIGTERM | 0 success, 2 bad config |
| Token usage | `usage` + `total_cost_usd` on the result | `turn.completed.usage`: `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`; app-server `thread/tokenUsage/updated` | `usage{...}` + `modelUsage{}` + `total_cost_usd` in the json result; on disk `~/.grok/sessions/<cwd>/<id>/signals.json` | session JSONL `goal_usage_attribution` records with `input/output/cached/reasoning_tokens`; MSP `session/tokenUsage` |
| Session/thread id + resume | `session_id`; `--resume <id>` from any directory | `thread.started.thread_id`; `exec resume <id>` / `resumeThread()` | `--session-id`, `--resume`, `--continue`, `--fork-session` | `stream.id`; `--session-id`, `resume`, MSP `session/fork` |
| Deny tools | `--disallowedTools` (comma **or** space separated), `--permission-mode dontAsk`, `--permission-prompts none`, `--strict-mcp-config` | sandbox + approval policy; per-turn `sandboxPolicy` / `approvalPolicy` | `--disallowed-tools` (removes), `--allow`/`--deny` rules `ToolPrefix(glob)` (gates), `--tools` allowlist | `--disable-write`, `--disable-shell`, `--disable-web-tools`, `--approval-mode`, `--permission-profile` |
| Explicit cwd | **no `--cwd`** — process cwd, plus `--add-dir` | `--cd`, or `cwd` on thread/turn | `--cwd` | `--workspace` (no `--cwd`) |
| Explicit model | `--model` | `--model` / per-turn override | `-m/--model` | `--model`, `--reasoning-effort` |
| Cancel | SIGINT ends the turn; SIGTERM aborts it; SDK `interrupt()` | app-server `turn/interrupt` | SIGINT/SIGTERM with documented codes; ACP `session/cancel` | MSP `turn/cancel`, `turn/interrupt`; signal behaviour UNVERIFIED |
| Structured final answer | `--json-schema` → `structured_output` | `--output-schema <file>` | `--json-schema` → `structured_output` | **none** |
| Built-in sandbox | Bash sandbox (bubblewrap/seatbelt) + whole-process sandbox runtime | Landlock/seccomp; `--sandbox read-only\|workspace-write\|danger-full-access` | `--sandbox <profile>`: `off\|workspace\|devbox\|read-only\|strict`, off by default | on by default; `--disable-sandbox`, `--sandbox-network` |
| Speaks ACP | via adapter | via adapter | **natively** (`grok agent stdio`) | no — own protocol over stdio |

## 2. What Foreman's shell shapes guess or lose today

Confirmed by reading the four adapters in this repo against the interfaces above.

1. **The Claude worker has no working directory.** No `--add-dir`, no `cd` in the wrapper, no `cwd=`
   on `Popen`, and `systemd-run --user` starts in the caller's home. The other two adapters pass
   `--workspace` / `--cwd`; this one passes nothing.
2. **Both "no usage counter" claims are wrong.** Grok prints `usage` and `total_cost_usd` in
   its json result and writes `signals.json` per session; Muse writes per-goal token attribution into
   its session JSONL. Both adapters return `None`.
3. **The Grok deny patterns are outside the documented grammar.** Rules are `ToolPrefix(glob)` and
   MCP tools are spelled `MCPTool(server__*)`, so the two bare `<server>__*` patterns name no prefix
   — likely a silent no-op (exact behaviour UNVERIFIED; the spelling is wrong either way). The
   reviewer denials `Write`/`Edit`/`Bash` are valid bare tool-name rules.
4. **`--yolo` turns off Muse's sandbox.** Muse ships sandboxed and approval-gated; `--yolo` disables
   approvals *and* the OS sandbox and trusts the workspace — and that sandbox is exactly what the
   symlink write escape found in review needed. `--approval-mode never` plus a permission profile
   gets the autonomy without losing the boundary. The adapter's note that no tool flag exists to withhold is
   also stale: `--disable-write`, `--disable-shell`, `--disable-web-tools`, `--permission-profile`.
5. **No structured completion anywhere.** Three of four vendors emit a machine-readable terminal
   event with a reason; Foreman greps a marker it echoes itself, which is why prose could match it.
6. **No session id captured**, so no run resumes after a kill and no vendor transcript correlates
   with a Foreman job. **No structured verdict** either: review jobs write a free-form file that
   `read_verdict` guesses at across six key spellings, while Claude and Grok take `--json-schema`
   and Codex `--output-schema`.
7. **`--collect` destroys the exit status.** Measured here: without it a finished transient unit
   still answers `Result=exit-code`, `ExecMainStatus=7`; with it the unit is gone and the same query
   returns `LoadState=not-found` with a default `Result=success ExecMainStatus=0`, so a failed job
   reads as success. This is why the marker line exists at all.
8. Not a bug: `--disallowedTools` accepts a space-separated single argument (`Comma or
   space-separated`, per that CLI's own help).

## 3. Agent Client Protocol

ACP is a JSON-RPC stdio protocol: `initialize`, `session/new` (absolute `cwd` + `mcpServers`),
`session/prompt`, `session/load`, `session/set_mode`, the `session/cancel` notification, client-side
`session/request_permission`, `fs/*` and `terminal/*`. A prompt returns a `stopReason` of `end_turn`,
`max_tokens`, `max_turn_requests`, `refusal` or `cancelled`, and a cancel **must** come back as
`cancelled`, not an error. Token usage arrives as a `usage_update` in `session/update` (`used`,
`size`, optional `cost`). Python, TS, Rust, Kotlin and Java client SDKs exist; a minimal Python
client is two callbacks plus a spawn helper.

Coverage: Grok is ACP-native; Claude and Codex have maintained adapters (`claude-agent-acp`,
`codex-acp`); Muse speaks its own protocol and is not on the agent list — three of four, not four.
The catch is that ACP standardises the *session*, not the *launch*: no standard way to select model,
reasoning effort, permission mode or sandbox profile at `session/new`, so those stay per-vendor
arguments on the process you spawn; and usage is agent-optional, so one ACP client still needs a
per-vendor fallback for the numbers.

## 4. Process supervision: what fleets actually do

Two camps, neither systemd. Human-attended tools give each agent a tmux session (claude-squad, Gas
Town, Agent of Empires) and detect completion by diffing captured pane output — the crudest signal,
chosen because it survives SSH drops. Headless orchestrators spawn plain subprocesses from the host
language's async runtime and read the protocol: Symphony (Elixir) runs `bash -lc` on the Codex
app-server and keys off `turn_completed`/`turn_failed`; Paperclip (Node `child_process`) keys off
exit code 0 with a `timeoutSec` kill and a 15 s grace window. Nobody surveyed uses supervisord — it
wants static config-file process definitions. Every serious system pairs its completion signal with a
**stall timeout**, because the common failure is no event at all, not a failure event; kill is
graceful-then-hard; where a worktree is involved the session is killed before the worktree is
removed. Worktree-per-worker is the near-universal isolation default, containers an optional layer.

Foreman's systemd choice is defensible and, once fixed, better than most of these — but three of its
own workarounds are unnecessary:

- `--working-directory=PATH` exists (measured working): no wrapper `cd` needed. And
  `--property=RuntimeMaxSec=` can enforce the job timeout instead of Foreman's own logic.
- Dropping `--collect` restores `Result` / `ExecMainStatus` as a real completion signal with a real
  exit code; `systemctl --user reset-failed` cleans up on the collector's own schedule.
- `setsid` is probably not needed. Cgroup membership is inherited across `fork` and `setsid`, and no
  source says setsid escapes a systemd cgroup; the observed kill is better explained by `KillMode`
  reaping the cgroup the moment the unit's main process exits — which plain `setsid` (fork-and-exit
  parent) triggers directly. `--service-type=exec` with no setsid makes the vendor CLI the main
  process, so unit lifetime is job lifetime, the cgroup is the kill group, and `systemctl --user
  stop` is the kill. The `setsid --wait` fix in the tree works, but it is a workaround for a
  self-inflicted cause. UNVERIFIED — measure before changing it.

## 5. Sandboxing per worker

Plain worktrees give correctness (no shared index), not containment: a symlink inside a worktree
redirects writes wherever it points, which is the escape already seen. Docker alone is a weak
boundary (shared kernel); the 2026 consensus is layered — kernel-level filesystem/network isolation
around the agent process, plus per-tool permission rules above it.

The cheapest fix covering all four vendors uniformly is a **process wrapper**: Anthropic's
`sandbox-runtime` (`srt`) wraps an *arbitrary* command in bubblewrap plus a seccomp filter on Linux,
configured with `filesystem.allowWrite`/`denyWrite` and `network.allowedDomains`. Vendor-agnostic, no
container, and it slots in exactly where `setsid bash -c` is today. Caveats: beta research preview,
config format may change, and on Linux the deny list is built once at launch so paths created mid-run
are uncovered; pin bubblewrap ≥ 0.12.0, which fixed a symlink-traversal write escape. Second-
cheapest, per-vendor: turn on what each already has — Muse's sandbox unless `--yolo` kills it, Grok's
`--sandbox read-only` (ideal for review jobs) or `strict`, Codex's `--sandbox read-only`.

## 6. Usage and quota

Per-session token counts exist for all four: Claude on the result message, Codex in
`turn.completed.usage`, Grok in the json result and `signals.json`, Muse in its session JSONL and via
`muse export` (Grok also gives USD cost and an opt-in OpenTelemetry export). Subscription-level
*quota* is the real gap: no documented API or file exposes the Claude Code 5-hour/weekly caps (an
open feature request asks for exactly that; the numbers appear only in an interactive session), so
Foreman's `meter` escape hatch stays necessary even once `usage` works for every pool.

## 7. Recommendation, ranked

**(a) Keep the shell shapes, with eight fixes — do this first.** Cost: about a day, no new
dependency, no change to the pool-as-a-directory contract. Fixes problems 1–4 and 7, plus the
escaping and setsid class outright.
(1) drop `--collect`, read `ExecMainStatus` as the completion signal, keep the marker as fallback; (2) add `--working-directory=<worktree>`, plus `--add-dir` for Claude; (3) add `--property=RuntimeMaxSec=` for the timeout; (4) replace the wrapper's `setsid` with `--service-type=exec` after one measured check; (5) add `--output-format json` (Grok) and `--json` (Muse), keep `stream-json` (Claude), so usage and session id are real for every pool; (6) fix the Grok deny spelling to `MCPTool(<server>__*)`; (7) replace `--yolo` with `--approval-mode never`, keeping Muse's sandbox; (8) wrap every worker in `srt`.

**(b) Move each adapter onto the vendor's SDK / JSON contract.** Cost: a supervisor daemon owning
long-lived processes, since the SDKs are libraries in *your* process rather than detached launchers —
a structural change to a one-shot CLI launcher; and Python SDKs exist for only two of four vendors,
so Grok and Muse stay CLI+JSON regardless. Fixes 5 and 6 properly (real events, real session ids,
schema-enforced verdicts) and gives cooperative cancel instead of a signal. Do it per-pool after (a),
starting with Codex, whose app-server is the richest of the four.

**(c) One ACP client for all.** Highest cost, lowest benefit here: covers three of four vendors,
still needs per-vendor launch arguments for model/effort/permissions/sandbox and a per-vendor usage
fallback, and trades four small argv builders for one client plus three adapter processes to install
and version. Its real payoff — permission prompts and diffs rendered in an editor — is not something
a headless swarm uses. Revisit if Foreman grows an interactive attach view, or a fifth ACP-native
vendor arrives.

Net: (a) now, (b) incrementally per pool, (c) not yet.

## URLs verified

- `code.claude.com/docs/en/{cli-reference,headless,agent-sdk/python,agent-sdk/overview,sandboxing,sandbox-environments}`; `github.com/anthropic-experimental/sandbox-runtime`; `github.com/anthropics/claude-code/issues/44328`
- `learn.chatgpt.com/docs/{non-interactive-mode,codex-sdk,app-server}`; `takopi.dev/reference/runners/codex/exec-json-cheatsheet/`
- `github.com/xai-org/grok-build`; `docs.x.ai/build/overview`; `github.com/meta-models/muse-code-sdk`; `developer.meta.com/ai/resources/blog/muse-code-new-plans-and-features/`
- `agentclientprotocol.com/{get-started/introduction,get-started/agents,protocol/overview,protocol/prompt-turn,protocol/session-setup}`; `agentclientprotocol.github.io/python-sdk/quickstart/`
- `github.com/{openai/symphony,gastownhall/gastown,paperclipai/paperclip,smtg-ai/claude-squad,agent-of-empires/agent-of-empires,manaflow-ai/cmux,andyrewlee/awesome-agent-orchestrators}`; `conductor.build/docs/core/parallel-agents`
- `manpages.ubuntu.com/manpages/jammy/man1/systemd-run.1.html`; `systemd.io/CGROUP_DELEGATION/`; `openwall.com/lists/oss-security/2026/08/27/7`
