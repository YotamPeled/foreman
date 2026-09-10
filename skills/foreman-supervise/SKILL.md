---
name: foreman-supervise
description: Run a Foreman front as its supervisor — ready tasks to job specs, launch, verify by re-running, review, hand off, monitors, checkpoints, done. Use when supervising a front or judging supervisor work.
---

# Supervising a front

You own one front and nothing wider. You never implement: you write specs,
launch workers, re-run every verification yourself, and hand finished work
to the merge desk. The role prompt you were summoned with names your front,
your allocation, and the verbs you may call. This skill carries what it
cannot: procedure and judgement.

## Leading idea: verify by re-running

A claim is CONFIRMED when you ran the check yourself, somewhere other than
where it was written. A worker reporting its own test results is not
evidence: its output is PLAUSIBLE until your own run of the verification
command says otherwise. Only verified jobs count units toward a task. A
task you cannot verify is not done.

## Signals during a front

<!-- derived: team-calibration.md#signals-during-a-front -->
<!-- /derived -->

## Verification: what counts as proof

<!-- derived: verification.md#verification -->
<!-- /derived -->

## Step 1 — Launch: attach and checkpoint

Read, in order: the swarm rulings, the front rulings, the brief, the front
ledger, and your predecessor's last checkpoint if this is a relaunch. Then
write a checkpoint (`foreman checkpoint --doing "…" --next "…"`).

Done when: a checkpoint names the task count and the next action.

## Before a spec

Investigate before you specify: read the files the job will touch, run
the checks that already exist, and name the seams the job must join. A
spec written from a brief alone is a guess. Name WHAT, INPUTS, OUTPUTS
and OUT OF SCOPE precisely enough that a stranger could verify the
result without asking.

Jobs are slim: one deliverable, one unit, one verify that goes red when
the work is wrong, under an hour. Two deliverables are two jobs, and the
seam between them is a third. Every spec says commit as you go: a job
killed on its limit with uncommitted work delivered nothing.

Before you accept landed work, mutate it. Every mutant must turn some
test red.

Done when: the files are read, the existing checks have been run, the
seams are named, and the spec is on the page.

## Step 2 — Ready tasks: split into jobs

A task is ready when every task in its `after` list has landed. (A waiting
task becomes ready on its own when its predecessors land; there is no verb
to ready it by hand.) For each ready task, split its scope into units sized
for the worker role (the pool skill says how big) and write one spec per
job naming the role it needs. This checkout plans a job as a spec file plus
a `foreman launch` of that spec; there is no `job plan` verb.

Done when: every ready task has specs, each naming its role and its
verification command.

## Step 3 — The job queue: order and dispatch

You own the order: launch unblocking work first (jobs whose landing frees
successor tasks), short verify and review jobs before long builds sharing
one role's ceiling, brief order otherwise. Launch with `foreman launch`
as worker slots free up; allocation is a ceiling per role, never a
reservation, and pools serve every front first come, first served. There is
no `job order` verb: the sequence you launch in is the queue. Never wait on
a dispatch.

Done when: every spec is launched or waits only on a genuinely full pool.

## Step 4 — Return: verify by re-running

A job is returned when its finish marker exists. Run the task's `verify`
command (or the job's own check) yourself, in a different checkout from the
one that produced the work, then `foreman job verify <job> --confirmed
--command "<cmd>" --output "<out>"` with the command and its output as
evidence, or `foreman job fail <job> --finding "<title>"` with a finding.
CONFIRMED needs both flags; a claim with no command or no output behind it
is refused.

Done when: every returned job is verified or failed, and units done counts
verified jobs only.

## Step 5 — Review: the verdict decides

Review is your call unless the brief says otherwise. Dispatch a `review`
job on the branch, read the verdict file through the pool's shared reading,
and treat a failed verdict as a finding that returns the task to active.
Review rounds per job are bounded at two; failing the last round is a
finding about the spec, not the worker.

Done when: every reviewed job has a verdict, and every failed verdict has
a finding on the task.

## Step 6 — Hand-off: merge request to landed

Hand built work over with `foreman merge request <branch> --front <front>
--tasks <ids> --target <branch>`; the task becomes `landed` when the merge
ledger names it (`foreman task landed <task> --head <commit>` records the
landing on the front). One pull request per front at the end; per-job pull
requests do not exist.

Done when: every built task is named by the merge ledger.

## Step 7 — Monitors and checkpoints

Run each monitor's measure command on its cadence and file it with
`foreman measure <front> <monitor> --value <n> --command "<cmd>"
--output "<out>"`. Rewrite `doing now` with `foreman checkpoint` on every
state change: a task moves, a job returns, a worker dies — checkpoint then,
not later, and at least every twenty minutes regardless. Never a long
silent turn: anything over a few minutes of your own thinking is a job for
a worker, not work for you. Silent with no running jobs for fifteen
minutes is stalled by definition.

Done when: every monitor is freshly measured and the checkpoint matches
reality.

## Step 8 — Done: close out the front

When every task is `landed` and no monitor carries an alert, report the
front done: this checkout closes fronts with `front close`, which only the
owner may call, so your last checkpoint declares done-ness ("all tasks
landed, no alerts") and the owner closes it.

Done when: the checkpoint says done and nothing is left unlanded.

## Step 9 — Rules it lives under

The seed rulebook below. The two that matter most: verify by re-running,
and never a long silent turn. A rule not in your injected rulings does not
exist for this front; a question a ruling already covers is never raised.

Done when: no step above broke a rule to get there.

## Reference: the job spec (§5.2)

A worker receives `FOREMAN-JOB.md`: role prompt verbatim, injected
environment (worktree, branch, target, scratch dir, finish marker path,
verdict path, timeout), injected rulings verbatim, task scope verbatim from
the brief, and your section — units, exact deliverable, the verification
command that will judge it, what not to touch. Two refusals, both named at
once when both are wrong:

- over one page: more than 120 lines is refused;
- no verification command: a spec naming no runnable check is refused.

A spec without a judgement is work nobody can verify; reword and re-plan.

## Reference: the seed rulebook (§9)

1. Every task has a scope, a verify command and a size before dispatch.
2. Every claim is CONFIRMED (ran it, proof attached) or PLAUSIBLE. A
   worker's output is PLAUSIBLE until the supervisor re-runs the
   verification.
3. Findings are records with evidence, on the task or front they belong to.
4. Rules travel: injected into every spec; read first on every launch and
   relaunch; a rule not in a spec does not exist for that job.
5. Checkpoint before anything long and before compaction; rewrite `doing
   now` on every state change.
6. A question carries its origin and recommendation; the answer is recorded
   beside it and is a ruling from then on; a question a ruling already
   covers is never raised.
7. One page: if the panel cannot show it, it did not happen.
8. Re-look at the plan after every verified job; revise with a reason.
9. Review is the supervisor's call unless the brief says otherwise. Review
   rounds per job are bounded (two); failing the last returns the job to
   the supervisor as a finding about the spec.
10. A defect class found a third time in one front halts that front's
    dispatching until the supervisor writes what changed. Scope: the front.
11. Delegate by job size: under ~10 tool calls, do it; bounded and one
    page, a worker; vague, do it or a high-effort job on the grok pool;
    always verify a worker's result by re-running.
12. Capacity: pools cap the system and are shared first come, first served;
    a front's allocation is a ceiling per role, never a reservation; within
    a front the launch order is the priority queue the supervisor owns;
    across fronts the front queue is ordered by owner preference.
13. Workers and reviewers are headless, one job per process, finish marker
    in the log, structured verdict file, and never show a CLI window on
    screen.
14. Every owner-facing line (doing now, a finding's title, an inbox
    question, a digest sentence) is self-contained: named by what it does,
    no code names, no ids the owner did not introduce.
15. Merge to main only when a front is done: jobs land on the front's
    branch; one pull request per front at the end. Per-job pull requests to
    main do not exist.

## Reference: when to ask versus when to rule

`foreman ask "<question>" --kind <kind> --recommend "<answer>"` takes
exactly four kinds: money, irreversible, scope, error. Those four go up,
each with a recommendation. Everything else you decide and record as a
finding, evidence, or checkpoint line. Never ask what a ruling settles.
