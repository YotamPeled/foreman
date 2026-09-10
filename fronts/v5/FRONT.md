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

### Decisions added 2026-09-10 after the survey of every past foreman, supervisor and desk (20 answers, ledger/survey-2026-09-10/)

22. A node's verify is proven able to fail before the node is accepted: the runtime runs it on the base
    (must be red) and on the head (must be green); a verify that was never observed red, or that does not
    resolve and run, is refused. The door refuses an EMPTY verify and never judges its vocabulary. A node
    states the property under test (what a wrong answer looks like), its verify may be revised with a
    reason, and its evidence scope is typed (source test / production load / fixture / live).
23. A target move invalidates recorded evidence: a green run, a review verdict, a measurement is bound to
    head plus base; the automatic rebase re-runs the check and marks prior verdicts stale. After a
    landing, the supervisor's branch is the landing's line and it re-cuts from there.
24. Map facts carry where and when they were seen and the commit; a derived fact inherits the weakest
    label of its inputs; a fact copied from something owned elsewhere gets a re-derivation leaf. Machine
    facts belong in the map: tunnels, port-forwards, credential paths, filesystem quotas, fixture
    containers, tested execution capabilities.
25. Shared mutable resources (fixture databases, locks, temporary filesystems) are reserved with a count
    like model slots; the runtime refuses to start a job past the count. Worktrees and worker scratch live
    on disk under the project, never on /tmp.
26. Silence is not a pass: a check that looked in the wrong place, a reviewer that died on quota or an
    access flag, a negative result without a positive control are distinct recorded states, never green.
    Both attempts of a retried review are kept.
27. The build that serves a verb or a verify is pinned (absolute interpreter in the MCP config and in
    per-repository policy) and recorded on every write; a role page is generated from the verbs the
    running build actually has.
28. The owner's word arrives only through the runtime: an inbox answer or a ruling on the ledger. A halt,
    approval or deletion relayed by a peer is a prompt to read the ledger, never an instruction.
29. The landing script refuses an empty range before running the check, re-runs the check AFTER the
    update on the tree that will land, records commits dropped by rebase, and a repository's policy may
    name a landing script (build, pin, dry run, rollout, assert) in place of a plain push or PR.
30. Muse joins the team from 2026-09-14 (back 09-10): three, model muse-spark-1.3-contributor at reasoning effort xhigh always (never high; rul-frzifag), for mechanical leaves only (one-page nodes whose
    verify is a number, a diff or a golden file), each unit idempotent so a death costs one unit; never
    the sole author of a node's only test; never on auth-shaped text; until the reset the runtime shows
    the pool out and Grok takes those leaves.
31. The foreman assigns each front's working team. The owner's team line is the default and the ceiling.
    The foreman derives the working team from the map and tree by the survey's calibration rules:
    builders = min(independently verifiable leaves, the supervisor's verification rate, shared-resource
    slots); model by the shape of each node's verify (number/diff -> cheap, behaviour/security -> strong
    plus an executing reviewer); reviewers only for behaviour and security nodes; repositories add landing
    lanes, not builders; leaves resting on assumed facts run alone and first. The derivation is recorded
    on the front and adjusted on the signals (verify backlog, a conflict resolved by judgement, two
    workers on one file, flakes, the same failure class twice, jobs returning under fifteen minutes). The
    owner is asked only when the derivation needs more than the quota.
32. Smaller amendments in scope: the collector owns the queue tick (or the design names the process);
    milestones carry their own verify; the rebase item precedes the landing item; reservations are
    phased (builders first, reviewers when a head exists); a pool out for days lets the foreman override
    "backup only after failure"; a first checkpoint is seeded at mint; refusals are appealable with a
    reason; a node may depend on another front's output; review rounds have a budget and after two rounds
    finding the same class the node returns to the supervisor for redesign; a flake register, and "green
    is a count, not a state" for checks run under contention; the door names the rejected field; node
    bodies are unbounded; a node kind for derived artifacts; role boundaries enforced by tool permission,
    not prose; a launch whose task title matches nothing is refused.
33. Every node names its break: the one-line change to the delivered code that must make its verify go
    red. Before a node counts as built the runtime, in the fresh worktree, runs the verify on the head
    (green), applies the break, runs it again (must be red), and restores; the outcome is recorded on the
    node beside the red-then-green of decision 22. A surviving break is not a failure by itself: the
    record goes to the supervisor, who classifies it under the mutation ruling (gap, ineffective, or
    vacuous test) before the node proceeds. A node with no break named is refused at the door. This is
    the last serial step of verification moved off the supervisor.

## Team

supervisor      opus-5:high:1        (Fable retired from the roster 2026-09-10, owner ruling: quota out)
builder         grok-4.6:high:2
builder         muse-1.3-contributor:xhigh:3          (mechanical leaves only; pool out until 2026-09-14)
backup-builder  opus-5:high:1        (only after a failed builder run)
reviewer        astra-6:low:1        (on the supervisor's call)
Muse is out until 2026-09-14 and takes nothing.

## Repositories

foreman   github.com/YotamPeled/foreman   base main   work branch v5   target main
Policy: check `python -m pytest tests -q` in a fresh worktree; direct push to main allowed for this
repository after a green check; commit trailers as the repository's CLAUDE.md states.
