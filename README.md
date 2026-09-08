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
- `src/foreman/` — version zero: the launcher, the collector, the rulings ledger, the inbox and `foreman status`.
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

Add `--window` to a launch you want to watch in a terminal. Without it a worker takes no desktop
workspace. `foreman rule`, `foreman ask` and `foreman answer` keep the rulings and the inbox; an answer
becomes a ruling in the same call. A verb called with a session id the roster does not know is refused,
and the attempt is recorded where the collector can see it.

## Status

Version zero runs. A real worker has been launched by the launcher, observed to completion by the
collector, and read off `foreman status`.

## License

MIT.
