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

## Status

Design stage. Nothing runs yet.

## License

MIT.
