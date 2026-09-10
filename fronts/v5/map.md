# Map — front v5 (written by the supervisor, 2026-09-10; revised the same night for decisions 22-32)

How to read: every fact is marked **[seen]** (I ran the command or read the line) or **[assumed]**
(inferred, not provoked). Refs are resolved against `origin` and the sha recorded. One section per
repository; this front has one. Paths are given relative to the checkout or by role (state directory,
config directory, install checkout, development checkout); this file is committed to a public
repository and carries no machine-specific path. Provenance (decision 24): every **[seen]** fact
below was observed on 2026-09-10 between 23:00Z and 00:30Z at `origin/main` 2058289 unless the fact
says otherwise; the command or file it came from is named in the fact. A derived fact inherits the
weakest label of its inputs.

## Repository: foreman (github.com/YotamPeled/foreman)

- `origin/main` = `2fc7685` (decisions 22-32 landed 2026-09-10 ~00:20Z) **[seen]** (`git pull --ff-only
  origin main`); the map's code facts were read at its parent `2058289403fe5cfaaee52db50ba1da79337826dd`,
  which touched nothing under `src/`, `tests/` or `bin/` (`git show --stat 2fc7685`) **[seen]**. Work branch `v5` does not exist on
  origin **[seen]** (`git ls-remote origin v5` printed nothing) nor locally. Target `main`.
- Package `foreman` 0.0.1, Python ≥ 3.12, zero dependencies, `src/` layout; 34 modules, 17,246 lines
  under `src/foreman` **[seen]**. Largest: `launch.py` 3,564, `collector.py` 1,871, `progress.py`
  1,240, `status.py` 1,038.
- **Two checkouts, two venvs [seen].** The installed `foreman` on PATH is a non-editable copy in the
  *install checkout*'s venv (that checkout is on `main`, never edited by hand; I am summoned into it).
  The *development checkout* holds the editable install and the only venv with pytest. The root
  `conftest.py` puts the checkout's own `src` first on `sys.path`, so the suite tests the tree it
  runs in whichever venv runs it **[seen]**. The collector unit runs the installed command with
  `FOREMAN_STATE`/`FOREMAN_CONFIG` set **[seen]** (`systemctl --user cat`).
- **Verify command.** `python -m pytest tests -q`. Green only when `python` is the development venv:
  891 passed, 1 deselected (the vendor smoke test), 50 s **[seen]**. Under the system python one test
  fails (`tests/v1/test_mcp.py::test_the_gate_reader_imports_the_modules_it_reads` spawns
  `sys.executable -c "from foreman import launch"`, which has no package) **[seen]**. Every job spec
  and the landing script must therefore run the check with the development venv's interpreter.
- **No CI, no CLAUDE.md, no `.github/` [seen]** (none in history either). "Per-push CI under eight
  minutes" (decision 21) has no enforcer today; the suite is the gate. Commit trailers are set by the
  harness, not by any repository file; several recent commits carry none **[seen]**.
- `.gitignore` excludes `FOREMAN-JOB.md`/`FOREMAN-ROLE.md` because they quote machine paths
  **[seen]**. `bin/foreman-proof trial-frictions` (1,416 lines, stdlib) runs that front's done-when
  clause by clause in an isolated world with fake vendors and never inherits the three FOREMAN
  variables **[seen]**; `bin/foreman-verify` serves only the panel front **[seen]**.
- Tests: 45 files, 767 test functions, per-file `env` fixtures setting `FOREMAN_STATE`,
  `FOREMAN_CONFIG`, `HOME` and clearing `FOREMAN_SESSION` **[seen]**; `tests/conftest.py` autouse
  fixtures close every systemd door (`collector._default_turn_spawn`, `headless._default_spawn`,
  `headless.free_unit_name`) and stub the machine process snapshot **[seen]**. Directory names
  `tests/v0`…`tests/v5` are the *old* runtime fronts; `tests/v5` is the headless-turns front, not
  this one **[seen]**. This front's tests go under `tests/front-v5/` (name chosen to avoid the clash).

### The runtime today, by area

**Storage.** `store.py` is the one writer: append-only `.jsonl` ledgers (fsync per line, one `flock`
on `<state>/lock` held per write, reads lock-free, torn last line tolerated) and atomic `.json`
snapshots (`roster.json`, `observed.json`, `collector.json`, `panel.json`) **[seen]**. Fold =
`fold_by_id`, last-wins by `id`, records without an id dropped (so `evidence.jsonl`, which has no
id, is read raw) **[seen]**. Paths come only from `paths.py`; the state tree is
`fronts/<name>/{front,tasks,jobs,evidence,findings,measurements}.jsonl`,
`sessions/<id>/{checkpoint.json,events.jsonl,turn.json,turns.jsonl,log,pid,verdict.json}`,
`slots.jsonl`, `pools.jsonl`, `rulings.jsonl`, `inbox.jsonl`, `merges.jsonl`, `anomalies.jsonl`,
`events/<day>.jsonl` **[seen]**. No schema integer; migrations are stdlib scripts under
`src/foreman/migrations/`, one exists (v1→v2 merge-mode backfill), markers in `<state>/migrations/`
**[seen]**. `Entity.from_dict` drops unknown keys, which is how old lines keep loading **[seen]**.

**Entities and states.** Front `queued|done` are the only written states (`active/halted/frozen`
declared, never written; freeze is a file) **[seen]**. Task `waiting→ready→built→landed`, `active`
never written; `after` names sibling titles; a successor is released only when every predecessor is
`landed` **[seen]**. Job `running→returned|returned-with-work|failed|killed→verified|history`;
`planned` and `queued` are declared and read by capacity/status but **never written**: the only job
minting site is `launch`, which writes `running` **[seen]**. No milestone, node, parent, tree,
depth or reservation exists anywhere in `src/foreman` **[seen]** (grep).

**Brief and front.** `front add <dir>` reads `brief.toml`: `name`, `want`, `done-when` (one
sentence), `land-on`, `merge` (`self|desk`), `order`, `prefer`, `after` (front names),
`[allocation]` role→int, `[[task]]` (title, scope with WHAT/INPUTS/OUTPUTS/OUT OF SCOPE, verify,
size, after, core, timeout, land-on), `[[monitor]]`, `[[rule]]` (not validated, not stored; re-read
from the copied brief at prompt time) **[seen]**. No key for goal, finish line, decisions, team or
repositories **[seen]**. The planner skill `skills/foreman-plan/SKILL.md` (216 lines) interviews
for want/done-when/tasks/verify/dependencies/allocation/monitors **[seen]**.

**Capacity.** Pool = directory with `manifest.toml` + `SKILL.md`; packaged `muse|grok|claude|codex`,
user pools under `<config>/pools/` replace a packaged one whole **[seen]**. Caps in `foreman.toml`
`[pool.<name>] cap|supervisor_cap|roles`; the live file says muse 0, codex 2, grok 6, claude 2
(supervisor_cap 2), fable 1 **[seen]**. Held slots are derived from `slots.jsonl` (open grant =
no `released_at`); `admit` checks and grants under the one lock; allocation per front is a
ceiling checked only at grant, never a reservation **[seen]**. Machine-wide count = processes whose
argv[0] basename equals the adapter binary, exempting anything resolving to `claude` **[seen]**.
Quota refusals (429 + a parseable reset) put a pool out in `pools.jsonl` until the reset; muse is
out until 2026-09-14 **[seen]**. Supervisors hold no job slot **[seen]**.

**Launch.** `launch <role> <pool> <spec>`: gate, validate (spec ≤ 120 lines and must name a verify
runner), mint session, `git worktree add -b <branch> <path> <base>` where `--base` is used **as
given, never fetched** (friction 14 open) **[seen]**; `--task` is looked up but an unknown title is
**not refused** (friction 15 open) **[seen]**; render role prompt + `FOREMAN-JOB.md` from
`templates/<pool>[.<kind>].md` and `job.md` (both-direction `{{field}}` check); roster `starting`;
`admit` slot; adapter spawns `systemd-run --user --unit=foreman-<sid> … bash run.sh` wrapping the
vendor with `### finished rc=$?` teed to the log; roster `running`; job line `running` with
`branch`, `base`, `worktree`, `log`, `timeout`, `units` **[seen]**. Workers get no MCP server;
supervisors get `<session>/mcp.json` naming `sys.executable -m foreman mcp` **[seen]**. Live defect
**[seen]** (foreman's wake): the launcher writes the *system* interpreter into that file for a
windowed supervisor, so my MCP server never connects; I use the CLI.

**Collector.** One systemd user unit, tick every 2 s under `collector.lock`: process snapshot,
per-session observe (transcript mtime, tree cpu, finish marker), roster and `observed.json`
writes; opens `supervisor silent|dead`, `job stalled|timeout|tail`, `intruder`, `unregistered
writer`, `collector stale`, `monitor stale|alert`; marks jobs terminal (unit result first, marker
second, branch-moved third) computing `head` at that moment; releases slots; emits one wake per
transition; relaunches a dead supervisor up to 2 per hour; spawns one detached `foreman turn`
carrier per headless session with pending wakes **[seen]**. Config is re-read every tick; there is
no reload signal; `collector stale` compares the code head/mtime recorded at start **[seen]**. The
live collector is flagged stale since 2026-09-09T23:08Z **[seen]** (`foreman status`); I may not
restart it (brief rule).

**Wakes and turns.** Per-session `events.jsonl`, reasons fixed by `WAKE_REASONS`; a wake is held
(not stamped) while `turn.json` says a turn runs; delivered only when a carrying turn succeeds;
heartbeat every 20 min; `foreman tell` is the foreman's message door **[seen]**. Windowed sessions
are never carried; I read mine with `wake next` **[seen]**.

**Verbs and MCP.** Registry = `cli.SUBCOMMANDS` via decorator; 49 leaf verbs; role gates are
`caller.check_role` calls inside handlers, and the role table shown in prompts is **read back out of
the source with `ast`** (`launch._module_gates`) **[seen]**. Three verb-module import lists differ
(`cli.main`, `launch.import_verb_modules`, `mcp._ensure_verbs`) **[seen]**. MCP = hand-written
JSON-RPC over stdio, tool inventory generated from the argparse tree, calls replay the CLI in
process, tools scoped by the roster role **[seen]**. Supervisor verbs missing versus DESIGN §12:
`task ready`, `job plan`, `job order`; foreman missing `admit`, `rule front`, `digest`, `route`
**[seen]**. `front add|list|prefer|close`, `cap`, `pool add|clone|remove`, `hook`, `freeze|thaw`
are owner-only (no role admitted) **[seen]**.

**Screen.** `status.render` prints header, needs-you, problems, working (per front: supervisor line,
allocation, evidence/findings, monitors, tasks with jobs, want/REMAINING/ESTIMATE/blocked), done,
job queue (`planned|queued`, so always empty today), merge queue, capacity, overall; the front
queue block is deliberately absent **[seen]**. `panel-feed` writes `panel.json` from the same
gatherers, rewritten after any writing verb **[seen]**. Golden test `tests/v0/status_expected.txt`
pins the text byte for byte **[seen]**.

**Landing.** The desk verbs (`merge request|take|land|fail`) still ship; the default merge mode is
`self` and `merge request` refuses on a self front **[seen]**. `merge land` is the only code that
rebases in a detached worktree, runs the check, pushes with lease to the target, and records
command/exit/seconds/output ref **[seen]**; it is desk-gated and reads its check from the global
`[merge] check` (or `[merge."<repo path>"]`) in `foreman.toml`, which on this machine names the
development venv's interpreter **[seen]**. `task landed --head` is the self path and records only
the sha **[seen]**. `job verify --run` executes a task's verify in a fresh detached worktree of the
job's head with the three FOREMAN variables stripped and records exit/seconds/output ref
**[seen]**.

**Job outcome and wait.** `wait <job|session>` polls the job ledger every 2 s to a terminal state and
prints outcome/exit/branch/head/log; exit 124 on `--timeout` **[seen]**.

**Doctor.** Eight read-only cross-checks (dead pids, orphan worktrees, stale slots, config pools,
collector staleness, front records lacking `merge`, CLI branch, default workspace) **[seen]**.

### The machine (facts that shape the plan, no paths)

- Roster: 154 sessions registered, 3 observed; one grok builder running for another front; the
  foreman session is live and windowed **[seen]** (`foreman status`).
- Pools live: grok cap 6 (ruled back to 3 when a trial ends), claude cap 2, codex 2, fable 1 (my
  own pool), muse 0 and out until 09-14 **[seen]**. Grok balance hit HTTP 402 once on 09-09
  (foreman's wake) **[seen]**; the backup builder is opus-5 high after a failed grok run (FRONT.md
  team).
- Nine job worktrees of the trial-frictions front are still attached to the install checkout's
  repository **[seen]** (`git worktree list`); 53 directories under `<state>/worktrees` **[seen]**.
- The install checkout is a git checkout on `main`; the collector's `collector stale` line is open;
  two other supervisors are dead and refused relaunch **[seen]** (`foreman status`).
- Disk 740 G free, 24 cpus, 30 G RAM **[seen]**.
- **`/tmp` is tmpfs; the state directory is on btrfs on disk; the project disk is ntfs3** **[seen]**
  (`df -T`). Decision 25 forbids worker scratch and worktrees on `/tmp`: today `job verify --run`
  and `merge land` build their detached worktrees with `tempfile.TemporaryDirectory` and
  `bin/foreman-proof` builds its world with `tempfile.mkdtemp` — all on tmpfs **[seen]**. Launch
  worktrees live under the state directory (disk) **[seen]**; past supervisors put job worktrees
  and logs under a scratch directory beside the repository on the project disk **[seen]**
  (trial-frictions job lines: worktree and log paths, timeout `45m` on 30 of 33 jobs).
- Shared mutable resources this front uses (decision 25): none beyond git and the state directory;
  every test and proof builds its own world **[seen]** (tests/conftest.py, bin/foreman-proof).
- No tunnel, port-forward, credential path or fixture container is needed by this front **[seen]**;
  `gh` is logged in for pushes **[seen]** (`gh auth status`).

### Gaps between today and each decision (what this front builds)

| decision | today | gap |
|---|---|---|
| 1 verbs = MCP, own front only | true for tools; MCP config names the wrong interpreter for a windowed supervisor | fix interpreter; scope reads to the caller's front |
| 2 `start --model --effort` | no `start`; collector via `collector unit`; foreman via `launch foreman` | new verb |
| 3 `front` with goal/finish/decisions/team/repos | `front add <dir>` reads want/done-when/tasks | new inputs, new validation, new record shape; old briefs stay readable |
| 4 map | prose file, no verb | `map` verb: append sections, resolve refs, fold on read, import from file |
| 5 milestones | nothing | `milestone` verb: add/split/merge with reason, history, change count |
| 6 tree, one door | tasks flat, jobs by launch | `node` verb with the six refusals; append-only; fold |
| 7 rendered worker page, role sheets | `job.md` from a spec file the supervisor writes | render from node + map facts + sheets; refuse a job with no sheet |
| 8 mechanical job queue | jobs born `running` by `launch`; `planned/queued` read, never written | `job queue|cancel|front|edit` for supervisors; the runtime (collector) starts jobs; reasons why each waits |
| 9 foreman queue | none | front queue items: start, stop/resume, land, rebase, relaunch, re-enable pool, checks, upgrade, clean, report |
| 10 quotas reserved per front | ceilings, never reserved | reservation table; start only when the whole team is reservable; reserved vs running on screen |
| 11 landing as a queued script | `merge land` desk-gated, global check | two-level landing item run by the runtime with per-repo policy, lock, record, finding on failure |
| 12 per-repo policy | `[merge] check` only | repository record with check, push/PR mode, trailers, PR body |
| 13 multi-repo | cwd is the repo | node names its repository; landing per repository |
| 14 planner skill | interviews for the old brief | rewrite for the v5 inputs; refuse an uncheckable finish line |
| 15 screen | old blocks | front queue with reservations, reserved/running, milestones with pieces and split count, tree, landings, base-moved |
| 16 plain files | true | keep |
| 17 observed vs declared | true | keep |
| 18 cutover after finish | n/a | old ledgers readable; migration only if needed |
| 19 files under fronts/v5 | this map | milestones.jsonl, tree.jsonl beside it; import verb |
| 20 frictions 14, 15 | both open | `--base` fetched and sha printed; unknown task refused; `job repoint` |
| 21 interim rules | rulings | followed by me until 11 lands |
| 22 verify proven red then green; property under test; typed evidence scope | `job verify --run` runs green only | door runs the verify on base and head; node fields `property`, `scope`; refuse empty verify, never judge vocabulary |
| 23 target move invalidates evidence | evidence is a raw ledger, unbound | evidence bound to head+base; rebase item marks stale and re-runs |
| 24 map provenance | this file | `seen_at`, `seen_where`, `commit`, `derived_from` on every fact; weakest-label inheritance; re-derivation leaf kind |
| 25 shared-resource slots; nothing on /tmp | tmpfs worktrees in three places | resource reservations with a count; worktrees and proof worlds on disk |
| 26 silence is not a pass | a check is exit 0 or not | check states: green, red, silent, died; both review attempts kept |
| 27 pinned build recorded on every write | MCP config names the wrong interpreter; writes carry `by` only | absolute interpreter probed; `build` {commit, interpreter} on every ledger line; role page from the running build's verbs (already generated) |
| 28 owner's word only via the runtime | true (inbox, rulings) | a process rule for me; a relayed halt is a prompt to read the ledger |
| 29 landing script details | `merge land` runs the check before the update | refuse empty range; check after the update on the landing tree; record dropped commits; policy may name a landing script |
| 30 Muse on mechanical leaves from the 14th | pool out; no leaf kind | node `mechanical` flag; queue routes muse only to those; idempotent units |
| 31 the foreman assigns teams | allocation from the brief | working team derived from map and tree, recorded on the front, adjusted on signals |
| 32 smaller amendments | — | collector owns the queue tick; rebase before landing; phased reservations; pool-out override; seeded first checkpoint; appealable refusals; cross-front deps; review budget and redesign state; flake register; door names the field; unbounded bodies; derived-artifact kind; tool-permission role boundaries; unknown task refused |

### Assumptions I am making (each marked, each cheap to overturn)

- **[assumed]** the development venv interpreter is the one every landing check must use; the
  repository policy (decision 12) will name it as `check = "<venv python> -m pytest tests -q"` in the
  machine's config, not in the repository.
- **[assumed]** the foreman will reinstall the CLI and restart the collector once per milestone at a
  safe point (brief rule); until then new verbs are proven with the checkout's `python -m foreman`
  from a job worktree against an isolated state directory, and the finish-line proof on the live
  installation runs only after the last reinstall.
- **[assumed]** old fronts keep running on the old shape throughout; every new ledger is a new file
  (`milestones.jsonl`, `tree.jsonl`, `queue.jsonl`, `reservations.jsonl`, `landings.jsonl`,
  `repositories.jsonl`) so no old line changes meaning.
- **[assumed]** the collector is the process that starts queued jobs and landings (decision 8 says
  "the runtime"); a separate daemon would need a second unit and the brief forbids me touching units.
