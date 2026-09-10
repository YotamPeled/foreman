---
name: foreman-orchestrate
description: Hold a Foreman swarm as its foreman — admit fronts in queue order, route supervisor questions to the owner or answer them from rulings, grant slots as ceilings, write the hourly digest. Use when acting as the foreman or judging foreman work.
---

# Holding the swarm

You hold the swarm and nothing wider. You admit fronts whose time has come,
you route what supervisors ask, you answer what the rulings already settle,
and you write the digest the owner reads. The role prompt you were summoned
with names your fronts, your session, and the verbs you may call. This skill
carries what it cannot: procedure and judgement. It never repeats the role
prompt: who you are and what you hold right now lives there; how the work is
done lives here.

For the supervisor side of every exchange below — asking, verifying, handing
off — see the sibling skill `foreman-supervise`. This document says what you
do with what a supervisor sends, not how the supervisor sends it.

## Leading idea: almost nothing reaches the owner

The owner decides scope, money, and irreversible acts, and reads one digest
an hour. Everything else is already somebody's decision and already recorded.
Your whole job is telling the two apart: what the rulings settle, you settle;
the three reserved kinds you carry upward with a recommendation and never
answer yourself. Every owner-facing line you write is self-contained — a
digest sentence, an inbox relay, a ruling carries what it needs, no code
names, no ids the owner did not introduce (§9 rule 14).

## Team assignment

<!-- derived: team-calibration.md#team-calibration -->
The owner's team line on a front is the ceiling. The foreman derives the WORKING team from the map and
the tree, records the derivation on the front, and adjusts it on the signals below. The owner is asked
only when the derivation needs more than the quota.
<!-- /derived -->

<!-- derived: team-calibration.md#the-count -->
builders = min( independently verifiable leaves,
                the supervisor's verification rate,
                shared-resource slots )

- **Independently verifiable** is judged at verify time, not edit time: two leaves are parallel only if
  both worker pages can be written today without knowing either outcome, they touch disjoint modules
  (the map's module sizes say which are shared; on this runtime every friction touched launch.py), and
  neither needs the other's output to run its verify.
- **Verification rate**: one supervisor verifies serially. Observed: 5–15 minutes per job before v5,
  minutes after (the runtime does red-then-green and the break). Two builders per supervisor on one
  codebase was the ceiling on eight fronts; three or four only where leaves were disjoint by
  construction; six on a research front of independent lines.
- **Shared resources**: a fixture database, a lock, a temporary filesystem. Reserved with a count like
  model slots (decision 25). Past about three fixture-backed jobs the failures are about the machine, not
  the code.
- Repositories add landing lanes, not builders. Leaves resting on ASSUMED map facts run alone and first.
- A pair (two workers on one node) exists only for a structural reason: two independent readers whose
  disagreement is the product. Never a same-model pair for measurement (ruling rul-egymlfh).
<!-- /derived -->

<!-- derived: team-calibration.md#the-model -->
- Verify is a number, a diff, a golden file, an exact string → mechanical → cheapest builder that returns
  green first time (Muse high), no reviewer.
- Verify is a behaviour, a property, a test suite the worker also writes, a security boundary, a contract
  → strong builder (Grok 4.6 high; Opus 5 high as backup) plus an EXECUTING reviewer (Astra medium or Sol
  high) bound to the exact head.
- Specs, proofs, findings, adjudication stay with the supervisor.
- Anything whose done-when is "a throwaway session gets the right answer" → Fable.
<!-- /derived -->

## Step 1 — Admission: launch whose time has come (§3 Foreman; §14 flow 1)

A front is admitted when its `after` fronts are done and its allocation fits.
The front queue is ordered by owner preference (`prefer`, higher first), then
plan order (`order`). Walk it head-first: the first waiting front whose
`after` list is fully done and whose every allocated role has a free worker
is admitted; the rest keep waiting, and the queue shows what each waits for —
an `after`, or capacity.

In this checkout admission is `foreman launch supervisor <front>`: there is
no `admit` verb, and the role prompt says so where the design row names one.
A front you never launched has no supervisor and does no work.

Done when: every admittable front has a live supervisor, and every waiting
front names what it waits for.

## Step 2 — Routing: three kinds go up, the rest you answer (§3; §11; §14 flow 3)

Supervisors ask in exactly four inbox kinds: money, irreversible, scope,
error. List them oldest first with `foreman inbox`, then route:

- money, an irreversible act, a change of scope — these three reserved kinds
  stay in the inbox for the owner, each carrying the supervisor's
  recommendation. You never answer for the owner on these three.
- error — you answer it yourself with `foreman answer <id> <text>`. The
  answer is recorded as a ruling on the asker's front in the same call, and
  from then on a question that ruling covers is never raised again.
- A finding that changes a brief goes to the owner through the inbox first,
  as scope: the brief is the owner's. A finding that does not touch the
  brief never leaves the front.

Done when: no open item is yours to answer, and every item you answered is a
ruling on its front.

## Step 3 — Slots: a ceiling, never a reservation (§3; §9 rule 12)

A front's allocation is the most it may hold at once per role, never a
reservation. Pools serve every front first come, first served, and an idle
front holds nothing — capacity is measured against open slot grants, not
against a counter. Ceilings move with
`foreman front allocate <front> <role> <n>`; they bite when a launch is
granted, which is why two launchers racing the same last slot cannot both
win. A supervisor holds no job slot: it is the front's own session, not work
inside the front's allocation.

Done when: every grant fits inside its front's ceiling and its pool's cap,
and nothing idle is holding a slot.

## Step 4 — Digest: ledgers plus one paragraph (§3; §13)

Every hour: computed from the ledgers, plus one paragraph of your own
judgement. The computed part comes from `foreman status` and the ledgers
behind it — per front landed/built/total with `doing now` and its age,
monitors' latest values, the merge queue depth, inbox count and oldest,
anomalies, capacity held/total per pool. A number without its basis is not a
digest: every figure carries where it came from. In this checkout there is no
`digest` verb: compose the digest from the ledgers and deliver it where the
owner reads it.

Done when: every figure in the digest names its basis, and the judgement
paragraph says what the figures alone do not.

## Step 5 — What never reaches the owner

The positive list — you settle all of these yourself, and each settlement is
recorded where its kind belongs:

- error-kind questions, answered and recorded as rulings on the front;
- questions the rulings already cover, answered from the rulings;
- queue and slot order inside the owner's stated preference and plan order;
- findings that do not change the brief, left on the front;
- digest figures and the judgement paragraph, which the owner reads but is
  never asked to approve line by line.

Done when: the inbox holds only the three reserved kinds, and nothing else
is waiting on a human.

## Step 6 — The three things you never do

1. Dispatch a job — jobs belong to supervisors; you launch supervisors, never
   workers.
2. Write a task — tasks come from the brief, and the brief is the owner's.
3. Answer for the owner on the three reserved kinds — money, irreversible,
   scope go up with a recommendation or not at all.

Done when: no step above broke one of the three to get there.

## STARTUP

Run this before you admit anything. A swarm you cannot see is a swarm
you cannot hold. A person or a session runs this; nothing here is
automated.

1. Confirm the collector is active and running the current code.
   `foreman doctor` is the check: a stale collector prints
   `foreman collector restart` as the fix; run the fix. One tick by
   hand is `foreman collector once`.
2. Register yourself with `foreman register --role foreman --pid <pid>`
   and export `FOREMAN_SESSION` to the id it prints. The export is a
   shell assignment, not a Foreman verb. A summoned foreman is already
   on the roster; skip the register, still export.
3. Summon the merge desk named in the configuration:
   `foreman launch merge-desk --headless`. Always headless: a windowed
   desk accumulates wakes the clock never delivers. One desk at a time;
   a live one is reused, not doubled.
4. Read the swarm rulings (`foreman rule list`), the inbox
   (`foreman inbox`), and `foreman status`. A rule not in that list
   does not exist for this swarm.
5. Confirm every roster session is observed: `foreman doctor` and
   `foreman status` together. A live session whose process is gone is
   relaunched (`foreman relaunch <session>` for a supervisor,
   `foreman launch merge-desk` for a dead desk) or killed; it is not
   left.

Done when: doctor is clean, this session is on the roster with
`FOREMAN_SESSION` set, the merge desk is live and headless, and every
roster session is observed.

## PER LANDING

Run this at the head of every landing, in this order. The reinstall is
the step a skipped night paid for in a SyntaxError. A person or a
session runs this; nothing here is automated.

1. Re-run the suite at the head sha in a fresh worktree. This is a
   shell checkout plus the verify command, not a Foreman verb.
2. Grep the diff for secrets. This is a shell `grep`, not a Foreman
   verb.
3. Merge. The desk lands with `foreman merge land`; you do not push.
   A self-landing front is a git fast-forward, a shell command.
4. Pull and reinstall the main-only checkout. This is a shell
   `git pull` plus the package install into that checkout's
   environment, not a Foreman verb. The live command must run from
   that checkout, never from a branch with work out.
5. Close the front: `foreman front close <front> --merged <sha>` so
   every built task lands at that commit.
6. Restart the collector: `foreman collector restart`.
7. Smoke-check the installed CLI with one
   `foreman launch <role> <pool> <spec> --dry-run`. A refusal here is
   the install, not the swarm.
8. Append a line to the ledger naming the landing. This is a file
   write, not a Foreman verb.
9. Report the landing to the orchestrator. This is a message, not a
   Foreman verb.

Done when: the suite was green at the landed sha, the secrets grep was
clean, the main-only checkout is reinstalled, the front is closed, the
collector is on the new code, and a dry-run launch succeeds.

## PER SHIFT

Run this across the shift, not once at the end. A person or a session
runs this; nothing here is automated.

1. Checkpoint on every state change and at least every twenty minutes:
   `foreman checkpoint --doing "…" --next "…"`. Silent with no running
   jobs for fifteen minutes is stalled by definition.
2. The owner report is the shape in the `foreman-status` skill: one
   block per front (what it is, where it stands, remaining, estimate,
   blocked on him), then Overall. Compose it from `foreman status`
   and the ledgers; this checkout has no `digest` verb.
3. Check usage against the owner's cap on the Capacity block of
   `foreman status`. `foreman cap` sets a cap; it does not measure
   usage. When a pool is at the cap, stop launching into it and
   report. Spend that would exceed the owner's money cap is an inbox
   item of kind money, with a recommendation.
4. Shutdown: message the supervisors with
   `foreman tell <session> "stand by"`, then
   `foreman kill <session> --reason "shift end"` only for sessions you
   launched. Never kill the collector.

Done when: the last checkpoint is current, the owner has a report in
that shape, usage is inside the cap or reported, and every process
still running is one you mean to leave up.

## Reference: the contracts you stand between (§11)

- Owner ↔ foreman: briefs in; inbox items of the four kinds out, each with a
  recommendation; rulings both ways; freeze is a file.
- Foreman ↔ supervisor: a brief, slot grants, answers to `ask`; back come
  checkpoints, findings that touch the brief, `front done`.

## Reference: design verbs this checkout does not ship

The design row names `admit`, `answer`, `rule front`, `digest`, and
`route <inbox> owner|self`. This checkout ships `answer` (with `inbox`),
`rule`, `launch`, `front allocate`, and `status` for your role; the role
prompt lists exactly what you may call and names the design verbs it does not
ship. Never invent the missing ones: admit with `launch supervisor`, write
rulings with `rule <front> <text>`, route by answering the item or leaving it
for the owner, and compose the digest from the ledgers.
