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
- **Only Claude sessions are interactive.** Codex, Grok and Muse run headless, one job per process, with a
  finish marker in the log and a structured verdict file. TUIs that ignore queued input are the mechanism
  behind off-roster launches.
- **Every memory has a named reader and a named moment of reading.** No vector store, no free-form
  "lessons" that agents write and nobody reads.

## Objects

| Object | What it is | Written by |
|---|---|---|
| **Component** | A lane of the product with a countable done-when: a checklist whose items include "merged" and "measured". Owned by one supervisor. | Orchestrator creates; supervisor maintains the checklist |
| **Job** | The atom. Spec + worktree + one worker + timeout → one artifact. Nothing exists below a job. | Supervisor dispatches via `launch-job` |
| **Session** | A running agent: role, model, workspace, slot, launched-by. Id minted by the runtime. | Launch scripts and collector only |
| **Rulings ledger** | Append-only, scoped swarm or component. Injected into every spec by the launch scripts; read first on every launch and relaunch. | Owner, orchestrator, supervisors |
| **Roster + slot ledger** | Who runs, with what, where, holding which slot, last observed activity. | Runtime only (launch scripts, collector). Sessions never write it |
| **Checkpoint** | Per session: doing, next, held, open questions. Collector derives the observed part; the session adds the declared part. `relaunch` reads it. | Collector + the session |
| **Ledger entries** | Per component: checkpoint, finding, evidence (CONFIRMED / PLAUSIBLE), question + answer. Stamped with the writing session. | Supervisors, orchestrator |
| **Merge ledger** | One line per landed change: what, why, which review, which head. | Merge desk |
| **Inbox** | The owner's. Money, irreversible acts, product scope. Each item carries a recommendation and its age. | Orchestrator, supervisors |

## Roles

- **Orchestrator** (Fable, interactive, summoned). Creates components, assigns supervisors, holds the slot
  grants, answers supervisors' questions, adds one paragraph of judgement to the hourly digest. Never
  dispatches jobs. Nothing depends on it being alive; when it is gone, decisions queue.
- **Supervisor** (Opus, interactive, one per component). First act: turn the done-when into a checklist.
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

## Scripts

`launch-orchestrator`, `launch-supervisor <component>`, `launch-job <component> <spec>`,
`launch-reviewer <job>`, `relaunch <session>`, `freeze`, `thaw`, `digest`.
Every launch: allocate a slot, mint the id, allocate a unique log and worktree, inject the environment
contract (where scratch space is, what is memory-backed, where checkouts go) and the applicable rules,
register the session. Worker launches deny every Foreman tool surface; assume any model will find any tool it
is not denied.

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
