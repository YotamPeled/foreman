# Foreman — system design

Status: full design, 2026-09-08. Supersedes the earlier piecemeal version. Nothing built yet.

Foreman is a runtime that runs a hierarchy of AI coding agents on one machine so that one human can
see all of it and it cannot outrun them. This document is the whole system: who uses it and how, every
entity and its fields, every contract between parts, every flow, every anomaly, what is on the screen
and where each number comes from.

---

## 1. Roles

| Role | What it is | How many | Lifetime | Interactive? |
|---|---|---|---|---|
| **Owner** | The human. Decides scope, money, irreversible acts. | 1 | — | — |
| **Planner** | A Claude session the owner opens to turn a want into a brief. Taught by `foreman-plan`. | one per planning conversation | ends when the brief is handed over | yes |
| **Foreman** | The orchestrator (Fable 5.1). Takes briefs, launches supervisors, grants slots, routes questions, writes the digest. Never plans, never dispatches jobs. | 1 | long-lived; can be absent — nothing depends on it being alive | yes — the only long-lived interactive session |
| **Supervisor** | Owns one component (Opus). Turns the brief's tasks into jobs, dispatches, verifies by re-running, measures monitors, reports. Never implements (rule 11 exceptions aside). | 1 per component | until the component is done | no (headless) |
| **Worker** | One process running one job in one worktree. Reads its spec, writes its artifact, exits. Never touches Foreman. | 1 per running job | the job | no |
| **Merge desk** | A supervisor with no component. Lands branches, writes the merge ledger. Never reviews. | 1 | long-lived | no |
| **Collector** | A daemon, no LLM. Observes, computes, flags, relaunches. | 1 | always | — |

Pools: one per model (`opus`, `codex`, `grok`, `muse`), each a plugin directory (§8) with a system-wide cap.
The `opus` pool has two caps: supervisors and workers (the "megalodon" for core logic and precision work);
both draw the same Claude quota. Worker roles, by allocation: **opus** for core logic and cut-throat
engineering; **muse** for everything else; **astra** and **grok** as reviewers.

---

## 2. How the owner uses it

```
want ──▶ foreman plan ──▶ planner interviews ──▶ brief.toml + plan.md ──▶ foreman component add ──▶ supervisor launched
                                                                                           │
   Super+M / foreman status  ◀── observed.json + ledgers  ◀── collector ◀── everything ◀────┘
   foreman answer / rule / freeze / kill / relaunch / slots ──▶ ledgers ──▶ supervisors read on next write
```

1. **Wanting something.** `foreman plan` opens a planner session. The owner talks architecture; the
   planner writes `brief.toml` (§4) and a short `plan.md` (the reasoning). The owner approves; the planner
   runs `foreman component add <dir>`; the planner's job is over.
2. **Watching.** `Super+M` (the panel) or `foreman status` (same text). Four questions, top to bottom:
   what needs me, what is wrong, what is working, what capacity is left. Plus the two queues.
3. **Acting.** Answer an inbox item, write a rule, freeze/thaw, kill or relaunch a session, change a
   pool's cap, prefer a component in the component queue, close a finished component. Each is one command or one key; each lands in a ledger as a
   stamped owner act.
4. **Not acting.** Everything not in the inbox is somebody else's decision and is already recorded.

Nothing enters the system except through a brief. Plans made elsewhere become briefs or they do not enter.

---

## 3. How each agent uses it

### Foreman
- On `component add`: validate (§4.3), write the component and its tasks to the ledger, put it in the
  **component queue**. Admission: a component's supervisor is launched when its `after` components are
  done and its allocation fits inside the pools' free capacity; the queue is ordered by owner preference,
  then plan order. Its allocation is reserved for it from launch to `done`.
- On a supervisor's question (`ask`): if it is money, irreversible or scope → inbox for the owner with a
  recommendation; otherwise answer it and record the answer as a ruling on the component.
- Every hour: `foreman digest` — computed from ledgers, plus one paragraph of judgement.
- On a finding that changes a brief (scope creep, wrong assumption): propose the change to the owner
  through the inbox; the brief is the owner's.
- Never: dispatch a job, write a task, answer for the owner on the three reserved kinds.

### Supervisor (per component)
1. **Launch**: read, in order, the swarm rulings, the component rulings, the brief, the component ledger,
   and its own last checkpoint if this is a relaunch. Write a checkpoint ("attached, N tasks, next: …").
2. **Ready tasks**: a task is ready when every task in its `after` list is `landed` (or `built`, if the
   brief says `after-built`). For each ready task, plan jobs: split the scope into units of work sized for
   the worker role (the pool skill says how big), write one spec per job (§5.2) naming the role it needs
   (`opus`, `muse`, or a reviewer), `foreman job plan` — the job enters the **component's job queue**.
3. **The job queue** is a literal priority queue the supervisor owns and edits (`foreman job order`): it
   decides what runs first. The launcher pops the highest job whose role has a free worker in the
   component's allocation, mints a session, creates the worktree and log, injects rules and environment
   into the spec, starts the worker. Nothing is declared per task about demand; everything is eventually
   served. The supervisor never waits on a dispatch.
4. **Return**: the collector marks a job `returned` when its finish marker exists. The supervisor runs the
   task's `verify` command (or the job's own check) itself → `foreman job verify <job> --confirmed` with
   the command and output as evidence, or `--failed` with a finding. Units done are counted from verified
   jobs only.
5. **Review** (optional, supervisor's call or the brief's `reviews = always`): dispatch a `review` job on the
   branch; read the verdict file; a failed verdict returns the task to active with a finding.
6. **Hand-off**: `foreman merge request <branch> --task <task> --target <branch>`; the merge desk lands it;
   the task becomes `landed` when the merge ledger names it.
7. **Monitors**: run each monitor's measure command on its cadence; `foreman measure`. Rewrite `doing now`
   on every state change (`foreman checkpoint`).
8. **Done**: when every task is `landed` and every monitor without an alert → `foreman component done`.
   The owner closes it.
9. **Rules it lives under**: the seed rulebook (§9). The two that matter most: verify by re-running, and
   never a long silent turn — anything over a few minutes is a job.

### Worker
Receives a spec file. Works in the worktree it was given. Writes its artifact (commits on its branch; for
a review job, a verdict JSON at the path in the spec). Writes the finish marker. Exits. It has no Foreman
tools; the launcher denies them. Its liveness is observed, not declared.

### Merge desk
Consumes the merge queue FCFS: rebase onto target, run the target's checks, land, `foreman merge land`
(head, target, review refs, tasks it lands) or `foreman merge fail` with a finding back to the supervisor.

### Collector (every 2 s)
Read the process table, worktree mtimes, CPU seconds, finish markers, vendor meters. Update `roster.json`
observed fields. Compute `observed.json` (§7). Detect anomalies (§10) and write them. Kill jobs past
timeout. Relaunch supervisors that are dead (process gone) from their checkpoint. Refuse nothing itself —
the CLI refuses; the collector reports.

---

## 4. The brief — the only input

### 4.1 Files
```
~/.config/foreman/components/<name>/
  brief.toml     the contract (below)
  plan.md        the planner's reasoning; read once by the supervisor
```

### 4.2 Schema
```toml
name      = "corpus"
order     = 2                     # the owner's plan order
want      = """what the owner asked for, in the owner's words"""
done-when = "one sentence the tasks add up to"
land-on   = "dev"                 # default merge target; a task may override
reviews   = "on request"          # none | on request | always
after     = []                    # components that must be done first
prefer    = 0                     # owner preference in the component queue; higher runs first

[allocation]                      # workers reserved for this component's supervisor, by role
muse  = 5                         # everything that is not core logic
opus  = 1                         # core logic, cut-throat engineering, precision
astra = 1                         # reviewer
grok  = 1                         # reviewer

[[task]]
title   = "second reads"
scope   = """
WHAT: read every app in the manifest a second time with a different model than the first read,
producing the same label schema.
INPUTS: manifest.json, labels/first/.
OUTPUTS: one label file per app under labels/second/.
OUT OF SCOPE: resolving disagreements (audit task). Apps with no first read are skipped and reported.
"""
verify  = "foreman-verify reads --stage 2 --min 600"   # exit 0 = done; the supervisor runs it
size    = 600                     # units of work; 1 for a single item
after   = ["manifest"]            # predecessors; empty = ready at start
timeout = "20m"                   # per job; defaults to the role's pool
land-on = "dev"                   # optional override
# no worker demand here: the supervisor splits the task into jobs, each job names its role,
# and jobs wait in the component's priority queue for a free worker of that role

[[monitor]]
question = "how many apps are collected?"
measure  = "corpus-count apps"    # prints a number; the supervisor runs it
unit     = "apps"
of       = 600                    # denominator, optional
every    = "job"                  # job | landing | 10m | 1h
alert    = "< 0.80"               # optional; puts it in Problems

[[rule]]
text = "Nothing on the injection topic goes to Grok."
```

### 4.3 Validation at `component add` (refused otherwise, every violation named at once)
- every task has `scope` with WHAT / INPUTS / OUTPUTS / OUT OF SCOPE, a `verify` command, `size`;
- `allocation` names roles that exist and does not exceed any pool's cap on its own; `after` names real
  components;
- `after` names real tasks and has no cycle;
- every monitor has `measure`, `unit`, `every`; `alert` parses;
- `done-when` is one sentence; `name` is unique; `land-on` is a branch that exists.

Two free monitors every component gets without declaring them: `doing now` (the supervisor's line, with
age) and progress (landed / built / total, rate, projected finish).

---

## 5. Entities

Every entity is a line in an append-only ledger (JSONL) or a field in an atomic snapshot (JSON). Ids are
short, runtime-minted, never invented by a session. Every line carries `at` (UTC) and `by` (session id).

### 5.1 Work
| Entity | Fields | State machine |
|---|---|---|
| **Component** | id, name, order, prefer, after[], want, done_when, land_on, reviews, allocation{}, supervisor (session), brief_path, state | `queued → active → done`; side states `halted` (rule 10), `frozen` |
| **Task** | id, component, title, scope, verify, size, after[], timeout, land_on, state, units_done, units_total | `waiting → ready → active → built → landed`; `built` = supervisor CONFIRMED all units; `landed` = merge ledger names it |
| **Job** | id, task, kind (implement, review, merge, research, verify), role (opus \| muse \| astra \| grok), priority, spec_path, session, worktree, branch, log, timeout, units (range), attempt, state, planned_at, queued_at, started_at, returned_at, verified_at, artifact, verdict_path | `planned → queued → running → returned → verified \| failed \| killed` |
| **Merge** | id, branch, tasks[], target, review_refs[], head, result, requested_at, landed_at, by | `requested → merging → landed \| failed` |

### 5.2 The job spec (what a worker receives)
Written by the launcher into the job's worktree as `FOREMAN-JOB.md`:
```
# Job <id> · <kind> · task "<title>" · component <name>
## Environment (injected)
worktree, branch, target, scratch dir (memory-backed, size), finish marker path, verdict path, timeout
## Rules (injected: swarm + component rulings, verbatim)
## Task scope (verbatim from the brief)
## This job (written by the supervisor)
units, exact deliverable, the verification command that will be run on it, what not to touch
```
A spec over one page is refused by `job plan`; a spec without the verification command is refused.

### 5.3 Sessions and capacity
| Entity | Fields |
|---|---|
| **Session** | id, role, pool, model, component, job, pid, launched_by, started_at, last_declared_at, last_observed_at, cpu_s, state (`running \| exited \| stalled \| killed`) |
| **Pool** | name, model, adapter, slots_total, kinds[], cannot_take[], timeout_default, meter, skill (config, §8) |
| **Allocation** | component, role, count — reserved at admission, released at `done` |
| **Slot grant** | pool, component, role, job, session, granted_at, released_at — the slot ledger; pool counts are sums over open grants; "idle" = allocated to a component, not granted to a job |
| **Frozen** | the presence of `~/.local/state/foreman/frozen` (file-presence-as-state) |

### 5.4 Records
| Entity | Fields | Named reader / moment |
|---|---|---|
| **Ruling** | id, scope (swarm \| component), text, source (owner \| foreman \| supervisor), acks[] | injected into every spec at launch; read first on every launch/relaunch |
| **Evidence** | on (task \| job), claim, status (CONFIRMED \| PLAUSIBLE), command, output_ref | whoever moves the task's state |
| **Finding** | on (task \| component), class, title, detail, evidence_ref | supervisor at the next plan re-look; foreman if it touches the brief |
| **Measurement** | monitor, value, of, status, command, output_ref | collector snapshots the latest per monitor |
| **Checkpoint** | session, doing, next, held[], questions[] (declared) + observed part (jobs running, last event) | `relaunch` |
| **Inbox item** | id, from, kind (money \| irreversible \| scope \| error), question, recommendation, options[], asked_at, answered_at, answer, answered_by, relayed (bool) | owner; foreman for routing |
| **Anomaly** | kind, subject, since, detail, resolved_at | panel Problems; collector; foreman |
| **Event** | at, kind, subject, data — the observed stream | collector; metrics |
| **Digest** | at, text, computed{} | owner |

---

## 6. Storage
```
~/.local/state/foreman/
  roster.json        snapshot: sessions (atomic rename)
  observed.json      snapshot: everything derived (§7)
  slots.jsonl        slot grants
  rulings.jsonl
  inbox.jsonl
  merges.jsonl
  anomalies.jsonl
  events/YYYY-MM-DD.jsonl
  frozen             presence = frozen
  components/<name>/ tasks.jsonl  jobs.jsonl  evidence.jsonl  findings.jsonl  measurements.jsonl
  sessions/<id>/     checkpoint.json  log
~/.config/foreman/
  foreman.toml       pools' slots, timeouts, anomaly thresholds, digest cadence
  components/<name>/ brief.toml  plan.md
  pools/<name>/      manifest.toml  launch  observe  meter  verdict  SKILL.md
  hooks/<event>/     executable scripts
```
One writer: the `foreman` CLI, under a lock held only for the write. Snapshots are staged and renamed.
Ledgers are appended. The panel and the collector watch the directory; every CLI verb also pokes them.

---

## 7. Derived numbers (`observed.json`, recomputed every tick, never stored elsewhere)
- per component: units landed / built / total; rate (landed units per hour over the last 2 h); projected
  finish; `doing now` + age; halted? frozen?; monitors' latest values and trends
- per task: state, units done, jobs running / queued, oldest queued wait
- per component: allocation by role — busy (job, doing what, running for) or idle; the job queue in the
  supervisor's order with the role each job waits for
- velocity, swarm-wide and per component: jobs finished / h, tasks finished / h, tokens / h per model
  (Fable, Opus, GPT from transcripts; Grok and Muse have no counter and show jobs / h with a mark)
- component queue: waiting components in preference order, what each waits for (an `after`, or capacity)
- per job: elapsed vs timeout, minutes since last file write, cpu_s
- per session: seconds since last declared write, since last observed activity
- per pool: slots held / total, queue depth, oldest wait, meter %, reset time, avg and p90 job duration
  (from verified jobs in the last 24 h)
- swarm: sessions registered vs observed, merge queue depth, inbox count and oldest, anomalies

---

## 8. Pools as plugins
```
~/.config/foreman/pools/muse/
  manifest.toml   model, vendor, slots, kinds = ["implement","research"], cannot_take = [], timeout = "20m", interactive = false
  launch          <spec> <worktree> <log> → starts the process headless, prints pid
  observe         <pid> <worktree> → JSON: transcript mtime, cpu_s, finish marker present
  meter           → JSON: percent, resets_at   (or exit 3: no meter)
  usage           <session> → JSON: input_tokens, output_tokens   (or exit 3: no counter)
  verdict         <path> → normalised verdict JSON (review jobs)
  SKILL.md        how a supervisor writes a spec this model does well; how to verify its output
```
Adding a pool is a directory. Foreman ships four. `foreman pool add/remove/list/clone` manage them.
Only `interactive = true` pools may open a window; only `opus` (supervisors) and the foreman are interactive
in the shipped config, and supervisors are not.

---

## 9. Seed rulebook (rulings ledger, swarm scope, present from first launch)
1. Every task has a scope, a verify command and a size before dispatch.
2. Every claim is CONFIRMED (ran it, proof attached) or PLAUSIBLE. A worker's output is PLAUSIBLE until the
   supervisor re-runs the verification.
3. Findings are records with evidence, on the task or component they belong to.
4. Rules travel: injected into every spec; read first on every launch and relaunch; a rule not in a spec does
   not exist for that job.
5. Checkpoint before anything long and before compaction; rewrite `doing now` on every state change.
6. A question carries its origin and recommendation; the answer is recorded beside it and is a ruling from
   then on; a question a ruling already covers is never raised.
7. One page: if the panel cannot show it, it did not happen.
8. Re-look at the plan after every verified job; revise with a reason.
9. Review is the supervisor's call unless the brief says otherwise. Review rounds per job are bounded (two);
   failing the last returns the job to the supervisor as a finding about the spec.
10. A defect class found a third time in one component halts that component's dispatching until the
    supervisor writes what changed. Scope: the component.
11. Delegate by job size: under ~10 tool calls, do it; bounded and one page, a worker; vague, do it or a
    Grok-high job; always verify a worker's result by re-running.
12. Capacity: pools cap the system; a component's allocation reserves workers by role; within a component
    the supervisor's job queue is a priority queue it orders; across components the component queue is
    ordered by owner preference. No other reservation exists.
13. Only the foreman is interactive. Every other session is headless, one job per process, finish marker
    in the log, structured verdict file.

---

## 10. Anomalies (collector; thresholds in `foreman.toml`)
| Kind | Condition | Auto-action | Shown as |
|---|---|---|---|
| supervisor silent | no declared write for 15 min AND no running jobs | none; relaunch offered | Problems |
| supervisor dead | process gone | `relaunch` from checkpoint | Problems until back |
| job stalled | running, no worktree mtime change and no CPU for 10 min | none; kill offered | on the task |
| job timeout | elapsed > timeout | kill; job `failed`; supervisor notified | on the task |
| intruder | a vendor process not in roster | none; kill offered | Problems |
| monitor stale | latest measurement older than 2× cadence | none | on the component |
| monitor alert | `alert` expression true | none | Problems |
| quota | pool meter ≥ 80 % | dispatch on that pool warns; ≥ 100 % refuses | Capacity |
| queue stuck | pool has free slots and a queued job waited > 1 min | launcher bug — surfaced | Problems |
| inbox aging | unanswered > 30 min | none | Needs you (age turns red) |
| unregistered writer | a CLI call with an unknown session id | refused | Problems |

---

## 11. Contracts, one line each
- **Owner ↔ Foreman**: briefs in; inbox items (money, irreversible, scope, error) out, each with a
  recommendation; rulings both ways; freeze is a file.
- **Foreman ↔ Supervisor**: a brief, slot grants, answers to `ask`; back: checkpoints, findings that touch
  the brief, `component done`.
- **Supervisor ↔ Worker**: `FOREMAN-JOB.md` in; commits on the branch + finish marker (+ verdict JSON) out.
  Nothing else in either direction.
- **Supervisor ↔ Merge desk**: `merge request` (branch, tasks, target, review refs); back: a merge ledger line
  or a finding.
- **Everyone ↔ Collector**: nothing declared to it; it observes. It writes `observed.json`, anomalies, and
  relaunches.
- **Panel ↔ state**: read-only on the state directory; every action is a `foreman` verb.
- **Pool adapter**: `launch`, `observe`, `meter`, `usage` (tokens, where the vendor exposes them), `verdict`
  executables with the argument/JSON shapes above.

---

## 12. Tooling by level
Every role gets exactly its verbs and nothing else, through two doors over one library: the `foreman` CLI
(owner, scripts, the collector) and one MCP server whose tool set is scoped by the caller's minted session
id (foreman tools for the foreman, supervisor tools for supervisors, merge tools for the desk; workers get
no server at all — the launcher denies it). Skills are per role the same way (§15).

Every summoned session receives a generated **role prompt** (`FOREMAN-ROLE.md`, from a template): who you
are, which component, who you report to, what you report and when (checkpoints, measurements, findings,
`ask`), your goal (the brief), your allocation, your tools, the rulings, the environment contract. No
session starts without one; nothing about its situation is left for it to guess.

### The `foreman` verbs (the whole mutation surface; every verb takes the caller's session from the environment)
| Caller | Verbs |
|---|---|
| owner | `plan`, `component add\|close\|list\|prefer`, `answer <inbox> <text\|option>`, `rule <scope> <text>`, `freeze`, `thaw`, `kill <session>`, `relaunch <session>`, `cap <pool> <n>`, `pool add\|remove\|clone\|list`, `status`, `queues`, `ledger <component>`, `doctor` |
| foreman | `admit <component>` (launches its supervisor, reserves its allocation), `answer`, `rule component …`, `digest`, `route <inbox> owner\|self` |
| supervisor | `checkpoint`, `task ready\|built`, `job plan\|order\|verify\|fail`, `evidence`, `finding`, `measure`, `ask`, `merge request`, `component done` |
| merge desk | `merge take\|land\|fail` |
| collector | `observe`, `anomaly`, `relaunch`, `kill` |
Unknown session id → refused by name. Wrong role for a verb → refused by name. Every refusal lists every
violated field at once. `foreman doctor` cross-checks observed against recorded: processes vs roster,
worktrees vs jobs, slot ledger vs process table, config vs schema.

---

## 13. What the panel shows, and where each line comes from
| Block | Line | Source |
|---|---|---|
| header | sessions registered/observed, frozen, collector age | roster.json, `frozen`, observed.json |
| Needs you | question · recommendation · kind · age · approve/decline/answer | inbox.jsonl |
| Problems | one sentence per anomaly · action | anomalies.jsonl |
| Working | per component: name · supervisor · landed/built/total · rate · left · `doing now` (age) | observed.json |
| | per task: title · state · units · jobs (model, worktree, elapsed/timeout) · after | tasks.jsonl, jobs.jsonl |
| | monitors: question · value/of · trend · measured N ago | measurements.jsonl |
| Job queue | per component, in the supervisor's order: job, role it waits for, waited | jobs.jsonl, slots.jsonl |
| Component queue | waiting components in preference order, what each waits for | components, allocations |
| Merge queue | merging, waiting, last landed with target and review | merges.jsonl |
| Capacity | per pool: held/total · meter · avg/p90 · waiting | observed.json |
Same rendering in `foreman status` (text) and the Quickshell panel (`Super+M`). Every key on the panel
maps to a CLI verb.

---

## 14. Flows
1. **New component**: planner → brief → `component add` → validate → ledger → component queue → `admit` when
   `after` done and allocation fits → role prompt generated → supervisor launched → reads rulings, brief,
   ledger → checkpoint → ready tasks → jobs into its priority queue → launcher pops as workers free.
2. **A task, end to end**: ready → jobs planned → queued → running (observed) → returned (finish marker) →
   supervisor verifies (CONFIRMED evidence) → optional review job → `merge request` → merge desk lands →
   task `landed` → component progress moves → monitor measured on `landing`.
3. **A question**: supervisor `ask` → foreman routes: reserved kinds → inbox with recommendation → owner
   answers → ruling on the component → supervisor reads it at next write. Other kinds → foreman answers →
   ruling. A question covered by a ruling is refused at `ask`.
4. **Supervisor dies**: collector sees the process gone → `relaunch` → new session reads rulings, brief,
   ledger, last checkpoint → continues; the old session's slots are released; the panel shows the gap.
5. **Freeze**: owner `freeze` → file → launcher refuses; running jobs finish or time out; collector flags any
   new vendor process as intruder. `thaw` removes the file.
6. **Job fails**: attempt 1 failed → supervisor may re-plan (attempt 2, tighter spec); attempt 2 failed →
   finding on the task; the supervisor does it itself (rule 11) or escalates.
7. **Component done**: all tasks landed, no monitor alert → `component done` → inbox (scope kind: accept?) →
   owner `component close`.

---

## 15. Skills shipped with Foreman (documents; cloneable like Omarchy configs)
| Skill | Reader | Must contain |
|---|---|---|
| `foreman-plan` | planner session | this design in brief; the brief schema and validation; what a sharp scope looks like (WHAT/INPUTS/OUTPUTS/OUT); what a good monitor is; pool strengths; the interview order; when a brief is ready |
| `foreman-orchestrate` | foreman | §3 Foreman; routing rules; slot granting; digest format; what never reaches the owner |
| `foreman-supervise` | supervisor | §3 Supervisor step by step; the rulebook; job spec template; verify-by-re-running; checkpoint discipline; when to ask |
| `foreman-pool-<name>` | supervisor, per pool | how to spec for this model; unit size; timeout; what it cannot take; how to read its output |
| `foreman-merge` | merge desk | landing procedure per target; the merge ledger line |

---

## 16. Build plan — Foreman builds Foreman
Three components, three briefs, three supervisors, run through the boxes board until Foreman can run itself:
1. **runtime** — state directory, CLI, validation, pools, launcher, collector, `status`, `doctor`. Testable
   with a fake state directory and fake pool adapters.
2. **panel** — Quickshell plugin rendering §13 from the state directory; `Super+M`; every key → verb.
3. **skills** — the five documents in §15, written against the CLI as specified here.
Then the first real swarm runs on it.
