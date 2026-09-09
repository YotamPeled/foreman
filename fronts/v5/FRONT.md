# Front v5 — the owner's inputs (v5 format)

Written 2026-09-10 from the owner's decisions of that night. This is the plan of record.
The adapter in briefs/v5/brief.toml exists only so today's launcher can start the front.

## Goal

Foreman starts a front from the owner's inputs alone (goal, finish line, decisions, team,
repositories), and from there the machine does every mechanical thing: the supervisor maps the
world, names milestones and grows a tree of jobs; two fully mechanical priority queues start every
job, landing and front; team quotas are reserved per front; landings are a script, not an agent;
the screen shows the queues, the reservations, the milestones and the tree.

## Finish line

Foreman v5 runs this front on itself end to end: this front's own map, milestones and tree are held
by the runtime through its verbs; the front's remaining jobs are queued by the supervisor and started
by the runtime; its job branches land onto the front branch and the front branch is queued onto main
by the foreman, both through the mechanical landing step; and `foreman status` shows the front queue
with reservations, this front's milestones with pieces done and the split count, and its tree — every
clause CONFIRMED by running on the live installation, with the proof recorded as evidence.

## Decisions (the owner's, 2026-09-10; each is a constraint, not a suggestion)

1. Everything is a CLI verb; the MCP tools are the same verbs; a session sees only its own front.
2. `foreman start --model --effort` brings up the collector and registers the foreman. No merge desk
   exists any more (retired 2026-09-10, rul-6hru7gd).
3. `foreman front` takes: supervisor model and effort; goal; finish line; decisions; team as
   `agent:effort:number:role` entries; one or more repositories, each with base branch, new work
   branch (must not exist) and target branch.
4. The map is written by the supervisor itself after reading the code and the machine, never by a
   helper from the goal alone. Every fact is marked seen or assumed. Branch refs are resolved against
   the remote and the sha recorded. One section per repository.
5. Milestones: five to eight, one list per front, written by the supervisor after the map. They may be
   added, split or merged later with a one-line reason; never renamed to hide a failure, never deleted.
   History is kept and the change count is shown.
6. The tree is a list of nodes each naming its parent; kinds milestone, task, job. One write door (the
   verb, also exposed as the MCP tool) refuses: a parent outside this front; a kind that takes no
   children (a job); a node without a verify command, a must-not-touch line or a reason; depth past
   the limit; a caller that is not this front's supervisor. Writes are append-only lines folded on
   read. Only the supervisor splits; a job never splits itself; a worker that finds a job too big
   returns with a finding. The owner never approves a step; only a finish-line change, money, or an
   irreversible act reaches him.
7. The worker's page is rendered by the runtime from the node (what, must-not-touch, verify), the map
   facts it names, the repository and branch. Each fixed role (builder, reviewer, supervisor) has a
   default sheet owned by the runtime; whoever queues may add to it; a full replacement needs a
   recorded reason; a job with no sheet is refused.
8. Queues are 100% mechanical. A supervisor may only queue, cancel, move to the front, or edit an
   unstarted job; it never launches. The runtime starts a job when its model has a free reserved
   slot, its dependencies have finished and the team policy allows (backup builder only after a
   failed run). The queue states why each job waits (no slot, dependency, lock). Script-only items
   (landing, proof) need no slot: only dependencies and a lock.
9. The foreman's queue: start a front (strict order; the front at the top starts when its whole team
   is reservable, nothing behind it starts first); stop and resume a front; land a finished front onto
   its target (the foreman decides when to queue it; a front marks itself done and requests); rebase
   every open front when its target moved (automatic by default; a front cannot finish while behind
   its target); relaunch a dead supervisor from its checkpoint; re-enable a pool at its reset time;
   nightly and merge-gate checks; upgrade Foreman itself at a safe point; clean worktrees, dead
   sessions and disk; write the owner report on a clock.
10. Team quotas: the foreman holds a quota per agent; a front reserves its team exclusively until it
    ends or is stopped; the foreman may not change a quota; when the top front cannot start and the
    machine has room it asks the owner with the numbers, and lowers the quota back when the front
    ends. The screen shows reserved versus running per pool. Vendor usage limits stay observed facts.
11. Landing is a queued script at two levels: a job branch onto the front branch, the front branch
    onto its target. It updates onto the target's remote head, runs the repository's check in a fresh
    worktree, pushes or opens the PR per the repository's policy under a lock, and records command,
    exit, duration, output file and resulting head. A failure reaches the requesting supervisor as a
    finding with the output attached, never the owner by default. A job with no recorded verify is
    refused.
12. Per-repository policy, read at landing: the check command, PR-with-review or direct push, commit
    trailers, PR body. Replaces the global check.
13. Fronts may span repositories: one map with per-repository sections, one milestone list, each node
    names its repository, landing and rebase run per repository.
14. The planner skill is rewritten for the v5 inputs and refuses to finish with any input missing or a
    finish line that cannot be checked. The foreman uses it for its own fronts.
15. The screen (status and panel) is rebuilt to the new shape as its own milestone: front queue with
    reservations, reserved versus running, milestones with pieces done and split count, the tree
    drawn from the same fold the checker uses, landings as queue items, base-moved warnings.
16. Storage stays plain files folded on read through the one door; a database only if reads become
    slow, behind the same verbs, invisible outside.
17. Observed versus declared is untouched: the collector reports what runs; the door admits only what
    is well-formed.
18. Cutover only after the finish line; old ledgers and briefs stay readable until the last old front
    closes.
19. Bootstrapping: until the verbs exist, this front's map, milestones and tree live as files under
    `fronts/v5/` in the repository, in the shape the verbs will read (`map.md`, `milestones.jsonl`,
    `tree.jsonl`), committed with the work; they are imported when the verbs land.
20. Frictions 14 and 15 from the orbit-v2 trial are in scope: `--base` resolves against the remote and
    prints the sha; an unknown task title is refused at launch and a finished job can be re-pointed.
21. Interim rules until the mechanical landing exists: land self (fetch the target from origin before
    every rebase, check in a fresh worktree, record head and output as evidence); slim jobs;
    investigate before the spec; a verify that goes red; commit as you go; tests never touch systemd
    or the live state directory; the installed CLI and collector are upgraded only by the foreman at a
    safe point; per-push CI stays under eight minutes.

## Team

supervisor      fable-5.1:high:1
builder         grok-4.6:high:2
backup-builder  opus-5:high:1        (only after a failed builder run)
reviewer        astra-6:low:1        (on the supervisor's call)
Muse is out until 2026-09-14 and takes nothing.

## Repositories

foreman   github.com/YotamPeled/foreman   base main   work branch v5   target main
Policy: check `python -m pytest tests -q` in a fresh worktree; direct push to main allowed for this
repository after a green check; commit trailers as the repository's CLAUDE.md states.
