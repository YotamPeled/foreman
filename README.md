# Foreman

A runtime for supervising a swarm of AI coding agents so that one human can see it and it cannot outrun them.

Foreman came out of a failed day: a swarm of one orchestrator, five supervisors and a rotating pool of
workers across four vendors, coordinated through a task board, that grew to 26 agent windows, burned over a
billion tokens, and spawned four builder sessions nobody had on the roster. The human lost sight of it and
froze everything. The failure was attention, not capacity: agents reported on themselves voluntarily, anyone
could launch anything, and a wedged agent looked exactly like a working one.

## Four ideas

1. **Observed beats declared.** A collector daemon watches processes, worktree file times, git heads, CPU
   seconds and finish markers. Sessions declare only what cannot be observed: what they are trying to do and
   what they wait on. The alarm is the discrepancy between the two.
2. **Identity is minted by the launcher.** Every session id is issued by the runtime at launch. No slot, no
   id; no id, no writes. The roster is true by construction, not by discipline.
3. **Slots are the capacity control.** Per-model counts granted by the orchestrator, shown on the page,
   pulled down by vendor usage meters (80% degrade, 100% block). Freeze is zero slots; the launch scripts
   refuse. There is no other kill switch and none is needed.
4. **The daemon is the clock.** A non-LLM loop reads the ledgers, relaunches dead supervisors from their
   checkpoints, flags anomalies and invokes a model once an hour for a digest. The orchestrator is a role a
   human summons, not a process that must survive.

## What lives here

- `docs/DESIGN.md` — the object model, the roles, the report-back contract, the seed rulebook, the page.
- `src/foreman/` — the runtime: the launcher, the collector, capacity, the progress verbs, the
  ledgers and `foreman status`.
- `src/foreman/pools/` — one directory per pool: how a session becomes a process.
- `briefs/` — the briefs fronts are added from.
- `packaging/foreman-collector.service` — the collector as a systemd user service.

## Running version zero

Version zero is Python 3.12 and the standard library, nothing else. Install it however you keep tools;
from a checkout, `pip install -e .` puts `foreman` on your path.

Two directories hold everything. State lives in `~/.local/state/foreman` and configuration in
`~/.config/foreman`; set `FOREMAN_STATE` and `FOREMAN_CONFIG` to put them elsewhere, which is how you try
this out without touching your real ones.

Start a worker, watch it, read the screen:

```
foreman launch muse muse /path/to/spec.md --front <name> --task "<title>" \
    --repo /path/to/repo --timeout 20m
foreman collector run          # or `once` for a single tick
foreman status
```

`launch` mints the session, builds its worktree and a log that is never reused, writes the job file the
worker reads and the role prompt it works under, starts the process, and records the job. `collector`
observes every two seconds and writes what it sees; it flags a silent supervisor, a stalled job, a job
past its timeout (which it kills), a job whose work is done but whose processes linger, an unclaimed
vendor process inside the swarm's own directories, and a ledger line written by a session nobody minted.
`status` prints one screen answering, in order, what needs you, what is wrong, what is working and what
capacity is left; `--fixture <dir>` renders any state directory, which is how the golden test reads it.

`foreman rule`, `foreman ask` and `foreman answer` keep the rulings and the inbox; an answer becomes a
ruling in the same call. A verb called with a session id the roster does not know is refused, and the
attempt is recorded where the collector can see it.

## Running version one

Version one adds the five things that turn the launcher and the screen into a runtime a front can be run
on: fronts come from briefs, supervisors are summoned by the same launcher as workers, capacity is
enforced, tasks move through verbs that demand evidence, and Astra reviews run on the Codex adapter.

A front starts as a brief — `brief.toml` with what the front wants, what done looks like, the branch it
lands on, an allocation, and its tasks with their scopes, verify commands and sizes:

```
foreman front add briefs/<name>       # validates the whole brief and names every violation at once
foreman front add briefs/<name> --fixture   # a front for trying the runtime out, marked as such
foreman front list
foreman launch supervisor <name>      # summons its supervisor: a window, a role prompt, a roster entry
```

`front add` refuses a brief rather than half-accepting one: a task with no verify command, a size that is
not a number, a wait on a task that does not exist, a cycle, a monitor whose measure or unit is missing,
a land-on branch that is not in the repository. Every violation is listed in one refusal. A summoned
supervisor is on the roster like any other session, and a second summon for a front that already has a
live one is refused by name — the supervisor is the only launch that opens a window, because workers and
reviewers are never on screen.

Two limits hold, and both are checked at launch under one lock:

```
foreman cap <pool> <n>                # the most sessions a pool may hold at once, across every front
```

The pool cap is the shared ceiling; the front's `[allocation]` is its own ceiling per role. Neither is a
reservation: pools are shared first come, first served, and a launch that would cross either limit is
refused with the numbers in the sentence. A supervisor holds no job slot and is counted against
`supervisor_cap` instead. Grants are an append-only ledger, and the collector gives a slot back when its
session returns, is killed, or disappears.

Work moves because a supervisor said so, with evidence behind it:

```
foreman job verify <job> --confirmed --command "<the check>" --output "<what it printed>"
foreman job fail <job> --finding "<what is wrong>"
foreman task built <task>             # only when every unit is accounted for
foreman task landed <task> --head <sha>
foreman task reset <task> --reason "<why it moved backwards>"
foreman evidence / foreman finding
```

A CONFIRMED claim with no command behind it is refused: running the check is what turns a claim into
evidence. Nothing is edited — every state change appends a revised copy of the record — and every verb
refuses a caller who is not the front's supervisor.

Reviews run as jobs on the `codex` pool with the `astra` role, and the reviewer's verdict is read from a
schema-checked file that is never the same path as the model's last message.

## Running version two

Version two is the rest of the runtime: work lands through a merge desk, a supervisor's verbs arrive as
MCP tools instead of shell commands, pools are directories anyone can add one to, monitors put numbers on
the screen, and `doctor`, hooks and migrations keep a machine honest between runs.

**The merge desk.** A front's supervisor no longer lands its own work unless its brief says so:

```
foreman merge request <branch> --front <name> --tasks "<title>" --target main
foreman launch merge-desk            # summons the desk, one at a time, in a window
foreman merge take <id>              # first come, first served by request time
foreman merge land <id>              # rebase onto the target, run the check, push, land the tasks
foreman merge fail <id> --reason "<why>"
```

Landing rebases in a worktree the desk opens for the branch and removes afterwards, so it never disturbs
the checkout anyone else is working in, and it runs the target's check from `[merge] check` in
`foreman.toml` before it pushes: the desk lands nothing it cannot check. A brief opts into the desk with
`merge = "desk"`; a front that says nothing lands itself with `task landed`.

**The MCP server.** `foreman mcp` speaks JSON-RPC over stdio and offers each connected session exactly the
verbs its role may call — read off the same gates the CLI refuses by, so the two can never disagree. A
summoned supervisor gets `--mcp-config <session>/mcp.json --strict-mcp-config` and its verbs as tools; a
worker launch carries no server at all and is offered nothing but `foreman version`.

**Pools as directories.** A pool is a directory with a `manifest.toml` and a `SKILL.md`. The four shipped
ones live in the package; a directory of the same name under `~/.config/foreman/pools/` replaces one
entirely, and a user copy whose manifest does not parse falls back to the packaged pool with one warning.

```
foreman pool list                    # name, model, where it came from, its roles
foreman pool clone <name>            # copy a packaged pool to edit it
foreman pool add <name> --from <dir>
foreman pool remove <name>           # a clone falls back to the packaged pool
```

**Monitors.** A brief declares them; the front's supervisor measures them, with the command behind each
number:

```
foreman measure <front> "<monitor>" --value 497 --of 497 --command "<the check>" --output "<what it said>"
```

They render under the front as `question — value/of · age · trend`, the collector flags one that has not
been measured in twice its cadence, and an alert expression is parsed, never evaluated.

**Keeping a machine honest.**

```
foreman doctor                       # every divergence between what is recorded and what is there
foreman migrate                      # each migration once, markers under the state directory
```

`doctor` cross-checks the roster against the process table, worktrees against jobs, slot grants against
running sessions, the configuration against the registered pools, and the collector against the checkout.
Each divergence prints the command that fixes it, and the exit code makes it a shell condition. Hooks are
executables you put under `~/.config/foreman/hooks/<event>/`; they receive the event as JSON on stdin, and
one that fails prints and is stepped over. A migration that cannot repair what it finds prints a notice
and exits 0, so nothing queued behind it is blocked.

## The installed command

The `foreman` on your path must never be whatever branch a working checkout happens to have out.
Two checkouts, two jobs:

- The **primary checkout**, on any branch, where work happens.
- A **main-only checkout**, with `main` checked out and never edited by hand, that the installed
  command runs from. Install it non-editable from there, so the running code is a copy of `main`
  and not a live view of a half-built branch.

A post-merge hook keeps the second true. Copy `packaging/foreman-post-merge` to the primary
checkout's `.git/hooks/post-merge`, make it executable, and set `FOREMAN_MAIN_CHECKOUT` to the
main-only checkout. On every merge or pull it fast-forwards the main-only checkout to the new
head, reinstalls the package where the merge touched its sources, and runs
`foreman collector restart`, so the collector re-records the new code and its stale line clears
with no hand restart. `foreman cap` and `front allocate` push the same restart after their own
write, for the same reason.

`foreman doctor` reports an installed command running off a branch as a divergence, with its
fix: reinstall from the main-only checkout, then restart the collector.

## Status

Version two runs. On this machine, on an isolated state directory: a fixture front was added with two
monitors and a merge target, a job ran on a fifth pool added as a directory, its task was verified and
built, the supervisor's own `task landed` was refused by name, the merge desk took the request and landed
it — rebase, check, push, task landed with its head — an on-land hook fired with the event on stdin, both
monitors rendered with their values and ages, `foreman doctor` named four divergences with a fix for each,
and a migration moved a v1 state directory forward and did nothing on the second run.

## License

MIT.
