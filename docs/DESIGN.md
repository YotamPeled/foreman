# Foreman design

Status: agreed in discussion on 2026-09-08. Nothing built yet.

## The problem it solves

A hierarchy of agents (one orchestrator, one supervisor per component, many workers, separate reviewers)
has a hierarchy of authority but, left alone, no hierarchy of attention. Agents report on themselves
voluntarily, anyone can launch anything, and the board is written to when the agent remembers. Visibility
becomes a side effect of good behavior and disappears exactly when an agent misbehaves. Foreman makes
visibility structural.

## Principles

- **Observed beats declared.** The page trusts the collector over the agent. Declared ≠ observed is the alarm.
- **Launch is the only door.** One launcher per role. It takes a slot, mints the id, opens the process with
  the environment contract and the applicable rules injected, and registers the session. No slot, no launch.
  An unknown id cannot write.
- **The ledger is the only channel.** No queued messages between agents, no chat. A rule written on a
  component reaches its supervisor on its next read; acknowledgement is a write.
- **Scripts do, models decide.** Every repeated act is a script; a model only chooses whether to invoke it.
- **Only the foreman is interactive.** The owner talks to one session: the foreman (Fable 5.1). Every
  supervisor, worker and reviewer runs headless, one job per process, with a finish marker in the log and a
  structured verdict file, and is seen only through the panel. No agent takes a desktop workspace. TUIs
  that ignore queued input are the mechanism behind off-roster launches.
- **Every memory has a named reader and a named moment of reading.** No vector store, no free-form
  "lessons" that agents write and nobody reads.

## Entities

### Actors — things with intent
| Actor | Count | Does |
|---|---|---|
| Owner | 1 | Orders components (the plan), answers the inbox, writes rules, freezes |
| Foreman | 1 | Creates components, assigns supervisors, grants slots, answers supervisors |
| Supervisor | 1 per component | Turns the done-when into tasks, plans and dispatches jobs, verifies returns, owns quality |
| Merge desk | 1 | A supervisor with no component: consumes the merge queue, lands branches, writes the merge ledger. Never reviews |
| Worker | 1 per running job | Headless; takes one job of a kind its pool allows |
| Collector | 1 daemon | Observes, computes, relaunches; never intends |

### Pools
One pool per model (Codex, Grok, Muse, Opus), each a plugin directory: slots, allowed job kinds, cannot-take
topics, default timeout, launch script, the skill that tells a supervisor how to spec a job for it, meter
source. A job requests a pool. The Opus pool is supervisors only and takes no jobs. Adding a pool is a directory.

### Work — the containment tree
```
Project ⊃ Component ⊃ Task ⊃ Job
```
- **Component**: the owner's unit of scope. Order (the plan), a done-when sentence, one supervisor.
- **Task**: one line of the component's checklist; the unit of progress. Written by the supervisor with a
  title, how to verify it (no verification, no task), a size (how many units it holds, 1 for a single item,
  N for a batch such as "second-read 600 cases"), and **its own worker pool**: which models may work it, how
  many slots at once, and the stage order when reads are chained (e.g. "first read: Muse, 6 slots; second
  read: Grok medium, 2 slots"). Slots are granted per task; the per-model header is the sum of lit slots
  across tasks, so the quota view and the task view are one truth. A task also declares its maximum
  concurrent jobs (1 = sequential) and its predecessors (tasks that must finish first); grants never exceed
  the maximum, and a task with an unfinished predecessor is drawn dimmed with its sockets closed, so the
  throttle and the order are both visible on the ring. This is the one ordering primitive; there are no
  arrows between boxes.
- **Job**: one dispatch to one worker, serving exactly one task. Kinds: `implement`, `review`, `merge`,
  `research`, `verify`. Carries spec, pool, worktree, timeout, verify command, slot, timestamps, artifact, verdict.
  Reviews are optional: the supervisor dispatches one or two review jobs only when it judges them worthwhile.

### Records — append-only, each with a named reader and a named moment of reading
| Record | Scope | Reader / moment |
|---|---|---|
| Rulings ledger | swarm or component | injected into every spec at launch; read first on every relaunch |
| Roster + slot ledger | runtime-owned; sessions never write it | launcher on every launch; collector every tick |
| Checkpoint | per session (doing, next, held, open questions); observed part by collector, declared part by session | `relaunch` |
| Findings | on a task or component | supervisor at the next plan re-look |
| Evidence | on a task state change, CONFIRMED (re-ran, proof attached) or PLAUSIBLE | whoever confirms or lands the task |
| Merge ledger | one line per landing: what, why, which review if any, which head, which target branch (feature, dev, main — normal git practice) | task state; duration metrics |
| Inbox | question + recommendation + answer; money, irreversible, scope only | owner |
| Events | the collector's raw observed stream | collector; metrics |
| Metrics | per component, declared in its done-when (name, unit, denominator if any); each entry: value, timestamp, CONFIRMED or PLAUSIBLE, the command that produced it. Appended by the supervisor after it ran the measurement — never self-reported by a worker | collector snapshots the latest per metric every tick; the panel draws them on the component's body (a fill against the denominator, a sparkline for a series) |

### Lifecycles
- Task: `open → active → built (supervisor CONFIRMED by re-running) → landed (a merge ledger line names it)`;
  drops back on a failed review. A returned-but-unverified job shows on its task with its age.
- Job: `planned (spec written) → queued (waiting for a slot) → running → returned (artifact + finish marker)
  → verified | failed`. Failed twice → a finding on the task, not a third job.
- Session: `minted → running → exited | stalled | killed`.

### Queues — each has a depth, an oldest-wait, and one named consumer
| Queue | Holds | Consumer |
|---|---|---|
| Task backlog (per component) | open tasks with no job planned | supervisor |
| Job lane (per component) | planned jobs | supervisor |
| Slot queue (per pool) | queued jobs waiting for a slot; first come, first served — no reservations by job kind, no priorities (a priority queue can come later) | launcher |
| Merge queue | branches handed to the merge desk | merge desk |
| Inbox | questions | owner |
| Anomalies | discrepancies | collector, then foreman |

A stalled queue points at exactly one role.

### Admission — when a thing is drawn
A thing appears on the page the moment its ledger line exists with its required fields; the fields are the
contract. Component: name, order, supervisor, done-when → an empty ring marked planning. Task: title,
verification, size, pool → an arc of the ring sized by its share, filled by its progress, with one socket per
slot (lit = job running, empty = capacity, none = frozen); jobs orbit their task wearing their model's glyph;
a chained task shows its stages so units waiting for a second reader are visible. Job: kind, task, pool, spec, timeout, verify command → in the lane; slot minted →
in a berth. Session: minted id → on the roster; in the process table without one → intruder. Planning done
anywhere else enters through the same CLI; there is no second path.

### Metrics — derived every tick, never stored
From timestamps on ledger lines and the event stream the collector computes, into one observed snapshot: per
component built / landed / velocity / projected finish; per job wait, run, verify-wait; per pool avg and p90
duration, slots held, queue depth; per supervisor silence and unverified returns; per queue depth and oldest.
One source, one computation, so no two surfaces disagree.

## Roles

- **Foreman / orchestrator** (Fable 5.1, the one interactive session). Creates components, assigns supervisors, holds the slot
  grants, answers supervisors' questions, adds one paragraph of judgement to the hourly digest. Never
  dispatches jobs. Nothing depends on it being alive; when it is gone, decisions queue.
- **Supervisor** (Opus, headless, one per component). First act: turn the done-when into a checklist.
  Then dispatch jobs, verify results by running them, write the ledger. Never implements; never runs a long
  silent turn (hand long work to a job, checkpoint first). Flips a checklist item only with CONFIRMED evidence.
- **Worker** (Muse; Grok only where its vendor allows the topic). Headless. Reads its spec and nothing
  else. Writes nothing to Foreman. Liveness is observed. Controls: slots and a per-job timeout the
  supervisor sets.
- **Reviewer** (Codex Astra medium; Grok 4.6 high fallback). Headless. Returns a structured verdict file,
  never a log to grep. Bound by the per-model "cannot take" list, checked at launch.
- **Merge desk**. One queue for the whole swarm with a reviewer pool. Components hand off branches and go
  back to building. Queue depth is a number on the page. Writes the merge ledger. Candidate for running on
  Opus as orchestrator, as an experiment.
- **Collector** (daemon, no LLM). The clock. Each tick: read observed signals, update roster and checkpoints,
  compute discrepancies, relaunch dead supervisors from checkpoint, refuse launches when frozen. Once an
  hour: invoke a model for the digest paragraph.

## Observed signals (free)

Process table and children, transcript/rollout file mtime, CPU seconds, git heads on the job's worktree,
window/service units, the finish marker, vendor usage meters where they exist (Claude and Codex expose
5-hour and 7-day percent; Grok and Muse expose nothing and are proxied by jobs completed per hour).

## Report-back contract

| Role | Writes | Liveness from |
|---|---|---|
| Worker | Nothing. Start and end are registered by the launcher and the finish marker. | Observed only |
| Supervisor | On every state change: job dispatched, job returned, decision made, checklist item confirmed. Checkpoint before anything long and before compaction. | Its writes plus its jobs' observed activity |
| Reviewer | Verdict file. | Observed |
| Orchestrator | Slot grants, component creation, answers. | Its writes |
| Collector | Roster, observed checkpoints, anomalies, digest. | Is the clock |

Stall rule: a supervisor with no active jobs and no write for N minutes is stalled by definition.

## Derived numbers

- **Jobs running** per model and component: launcher registrations minus collector-observed exits.
- **Component percent**: confirmed checklist items / total. Shown as built vs landed.
- **Velocity**: confirmed items per hour → projected finish. Velocity at zero is the earliest stall signal.
- **Slots**: per model, running vs granted vs quota headroom.

## Seed rulebook

Standing rules present from the first launch. Supervisors obey them; the page shows when they do not.

1. Every unit of work has a goal and a countable done-when before dispatch.
2. Every claim is CONFIRMED (the writer ran the check, proof attached) or PLAUSIBLE. A worker's output is
   PLAUSIBLE until the supervisor re-runs it.
3. Findings are records with evidence on the component they belong to, not chat.
4. Rules travel: a component rule binds whoever holds it next, a swarm rule binds everyone; reading the
   rules is the first act after launch or relaunch. For headless jobs, rules travel by injection into the spec.
5. Checkpoint before anything long and before compaction.
6. A question carries its origin; its answer is recorded beside it and is a rule from then on. A question a
   standing rule already covers is never raised.
7. One page. If the page cannot show it, it did not happen.
8. Re-look at the checklist after every finished job; revise with a reason on the page.
9. Review rounds per job are bounded (two). Failing the last round returns the job to the supervisor as a
   finding about the spec, not a third round.
10. A defect class found a third time in one component halts that component's dispatching until the
    supervisor writes what changed. Scope is the component, never its parent.
11. Delegate by job size. A supervisor does a tiny job (under about ten tool calls) itself; hands a
    clear, bounded job that fits one page of spec to a Muse worker; does a vague job ("find out why this
    fails", "redesign X") itself or as a Grok 4.6 high job within budget; and always verifies a worker's
    result itself by re-running. Why: doing a job itself puts every read and command into the
    supervisor's context (50k–150k tokens per repair round, then a compaction that loses rulings);
    delegating costs about 15k–25k (spec, verify, one relaunch when the worker hangs) and moves the heavy
    work to the worker's subscription. The gain exists only when the spec is tight.

## Scripts

`launch-orchestrator`, `launch-supervisor <component>`, `launch-job <component> <spec>`,
`launch-reviewer <job>`, `relaunch <session>`, `freeze`, `thaw`, `digest`.
Every launch: allocate a slot, mint the id, allocate a unique log and worktree, inject the environment
contract (where scratch space is, what is memory-backed, where checkouts go) and the applicable rules,
register the session. Worker launches deny every Foreman tool surface; assume any model will find any tool it
is not denied. `launch-job` refuses a spec over about one page and requires the verification command in the
spec, so rule 11 is enforced at the door rather than remembered.

## The page

One screen, phone-readable, built on an existing component kit (NiceGUI; see the research notes).

- **Anomaly strip** at the top: unregistered writer, declared ≠ observed, quota past 80%, question older
  than 30 minutes, stalled supervisor, halted component. If nothing floats, nothing is wrong.
- **Inbox**: decisions and errors, each with recommendation and age.
- **Plan**: the components in the owner's order. This is the owner's; the checklists under them are the
  supervisors'.
- **Components**: percent (built / landed), velocity, next milestone, blocker, supervisor. Expand → jobs.
- **Sessions**: role, model, workspace, doing-now, minutes silent, slot. Header: per model, running vs
  granted vs quota.

Owner controls on the page, each landing in a ledger as a stamped owner act: write a rule, freeze / thaw,
kill or relaunch a session, edit the plan.

## Open questions

- Whether the supervisor tier collapses for mechanical components (the merge-desk-on-Opus experiment).
- Worker timeout: the supervisor sets it per job; whether the collector should also stall on
  "no file touched for M minutes" independent of the wall clock.
- Exact slot numbers at first launch; slots are grants and the meters pull them down, so start generous.
