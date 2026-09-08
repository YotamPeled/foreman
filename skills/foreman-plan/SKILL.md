---
name: foreman-plan
description: Turn an owner's want into a brief.toml plus plan.md that passes foreman front add on the first try. Use when opening a planning conversation, interviewing the owner, writing task scopes, choosing monitors and allocation, or deciding a brief is ready to hand over.
---

# foreman-plan — the planner's skill

You are the planner. One conversation with the owner goes in; a directory
holding `brief.toml` (the contract) plus `plan.md` (your reasoning) comes
out. `foreman front add` admits it on the first try, or this skill failed.

## 1. The world in one page

Chain of sessions: **owner → planner → foreman → supervisor → worker**.
You are the planner.
The owner wants something and approves the brief. You write the brief. The
foreman validates it, ledgers it, and launches its supervisor. The supervisor
splits tasks into jobs and verifies each by re-running. The worker runs one
job in one worktree, commits, writes its finish marker, exits.

Hierarchy of work: **project > front > task > job**. A front is one brief,
owned by one supervisor. A task is one scoped unit with its own verify
command and size. A job is one worker's slice of a task. Nothing enters the
system except through a brief.

## 2. Interview order

Ask in this order; each answer fills one part of the brief. Do not write the
files until every answer exists.

1. **Want** — what does the owner want, in their words? (brief `want`)
2. **Done-when** — one sentence the tasks add up to. (brief `done-when`)
3. **Tasks** — what are the pieces, and what does each exclude?
   One scope per task, each with WHAT / INPUTS / OUTPUTS / OUT OF SCOPE.
4. **Verify and size** — per task: the command whose exit 0 means done,
   and the size in units of work (1 for a single item).
5. **Dependencies** — which tasks wait on which (`after`, by title).
6. **Allocation** — ceiling per worker role (step 6).
7. **Monitors** — one question with a number per thing the owner will ask
   about while it runs (step 5).
8. **Rules, reviews, landing** — standing rules, `reviews`, `land-on`,
   `order`, `prefer`, front-level `after`.

Completion criterion for the interview: every slot of the recipe in step 3
is filled from something the owner said, not something you guessed. A slot
you filled by guessing is marked `TODO(owner)` and the brief is not ready.

## 3. Recipe: the brief file

Fill every REQUIRED slot. Field-by-field rules follow in steps 4–6;
the pre-handover checklist in step 8 is the same rules as checks.

```toml
name      = "front-name"            # REQUIRED: unique, directory-safe
order     = 1                       # plan order, integer
want      = """owner's words"""     # REQUIRED, non-empty
done-when = "One sentence."         # REQUIRED: exactly one sentence
land-on   = "main"                  # REQUIRED: a branch that exists
reviews   = "on request"            # none | on request | always
after     = []                      # fronts already on the ledger, by name
prefer    = 0                       # queue preference, integer
merge     = "self"                  # self | desk | absent (code rule)

[allocation]                        # REQUIRED table: ceiling per role
muse  = 2
opus  = 1
astra = 1
grok  = 1

[[task]]                            # at least the fields below
title   = "first work"              # REQUIRED, unique across tasks
scope   = """                       # REQUIRED: WHAT, INPUTS, OUTPUTS,
WHAT: ...                           #   OUT OF SCOPE, all four headings
INPUTS: ...
OUTPUTS: ...
OUT OF SCOPE: ...
"""
verify  = "pytest tests -q"         # REQUIRED: exit 0 means done
size    = 3                         # REQUIRED: positive integer
after   = []                        # task titles in this brief
timeout = "20m"                     # optional string, per job
core    = false                     # optional boolean: opus-only work

[[monitor]]                         # optional, repeatable
question = "how much is done?"      # the owner's question, in words
measure  = "count-things"           # REQUIRED: prints a number
unit     = "things"                 # REQUIRED: what the number counts
of       = 100                      # optional denominator
every    = "landing"                # REQUIRED: job | landing | 10m | 1h
alert    = "< 0.80"                 # optional: operator plus number

[[rule]]                            # optional, repeatable
text = "Standing rule for this front."
```

## 4. Fields the validator checks

The code's rules win over any looser description. Each rule below is a
check in step 8; the step-8 wording is the one you run before handover.

- `name`: non-empty, directory-safe (`[A-Za-z0-9][A-Za-z0-9_-]*`),
  unique across fronts already on the ledger.
- `want`: non-empty string. Keep the owner's phrasing.
- `done-when`: exactly one sentence — a single line ending in `.`, `!`,
  or `?` with one sentence boundary in it.
- `land-on`: non-empty, and the branch must exist in the repo. The brief
  directory must sit inside a git repository or the check itself fails.
- `merge`: absent, `self`, or `desk`. Anything else is refused.
- `order`, `prefer`: integers when present.
- Front `after`: a list; every entry names a front already on the ledger.
- `allocation`: a table; roles are exactly opus, muse, astra, grok —
  any other name is refused. Counts are non-negative integers. The
  validator does not check pool caps; keep each ceiling modest anyway.
- Task `title`: required, and no two tasks share one.
- Task `scope`: required, and must contain all four headings WHAT,
  INPUTS, OUTPUTS, OUT OF SCOPE — literally, as substrings.
- Task `verify`: required, non-empty. The validator does not check that
  the command exists: you check it by running it yourself.
- Task `size`: a positive integer (`core` marks opus-only work and must
  be true/false when present; `timeout` and task `land-on` must be
  strings when present).
- Task `after`: a list of titles in this brief; unknown titles and
  dependency cycles are refused.
- Monitor `measure`, `unit`, `every`: each a non-empty string.
  Monitor `alert`: an operator plus a number (`< 0.80`, `>= 5`, `!= 0`).
- The file must be valid TOML holding one table, named `brief.toml`
  in the directory you hand over.
- `reviews` accepts anything; write none, on request, or always.

## 5. A good monitor

A good monitor is one owner's question answered by one number printed by
one command. Write each monitor as that triple:

- **Question**: what the owner will ask mid-run ("how many are collected?").
- **Number**: the value with its unit and optional denominator (`of`).
- **Command**: `measure` prints the number; the supervisor runs it on the
  `every` cadence and flags `alert` expressions and stale monitors
  (no measurement for twice the cadence).

A monitor whose measure needs a paragraph of interpretation is two
monitors. A monitor with no question behind it is deleted.

## 6. The four pool roles

- **opus**: core logic, cut-throat engineering, precision. Only role that
  may take a task marked `core = true`, and the backup for a job Muse
  died on twice. Smallest ceiling; spend it where correctness is the work.
- **muse**: everything that is not core logic. The default worker role
  and the largest ceiling.
- **astra**: reviewer. Reads a branch and returns a verdict; never builds.
- **grok**: reviewer with a different model's eyes; never first-run
  building, never injection or security topics. Every front carries a
  grok ceiling of at least 1 so the backup-builder rule can run.

## 7. A sharp scope

WHAT states the change, INPUTS names what the worker reads, OUTPUTS names
the artifact, OUT OF SCOPE names the nearest thing the worker must not do.
Sharp:

> WHAT: read every app in the manifest a second time with a different
> model than the first read, producing the same label schema.
> INPUTS: manifest.json, labels/first/.
> OUTPUTS: one label file per app under labels/second/.
> OUT OF SCOPE: resolving disagreements (the audit task). Apps with no
> first read are skipped and reported.

Blunt (same intent, refused by this skill if not by the validator):

> WHAT: improve the labels.
> INPUTS: the repo.
> OUTPUTS: better data.
> OUT OF SCOPE: none.

The blunt scope fails three checks: WHAT names no observable change,
INPUTS/OUTPUTS name no files, and OUT OF SCOPE refuses nothing — so two
workers would build two different things. Rewrite until a second planner
could not split the task differently.

## 8. Pre-handover checklist

Run every check; the brief is ready only when all pass. Each names the
refusal it prevents.

1. Scope check: every task scope contains WHAT, INPUTS, OUTPUTS and
   OUT OF SCOPE.
2. Verify check: every task has a non-empty `verify` command, and you ran
   each one yourself — the validator never checks a command exists.
3. Size check: every task `size` is a positive integer.
4. Role check: `allocation` names only opus, muse, astra, grok, with
   non-negative integer counts, grok at least 1.
5. Front-order check: front `after` names only fronts already on the
   ledger; `order` and `prefer` are integers.
6. Task-order check: task `after` names only titles in this brief, no
   title twice, and the task `after` graph has no cycle.
7. Monitor check: every monitor has `measure`, `unit`, `every`; each
   `alert` parses as operator plus number.
8. Sentence check: `done-when` is one sentence on one line.
9. Identity check: `name` is directory-safe and unique, `want` is
   non-empty, `merge` is absent, self, or desk.
10. Branch check: `land-on` names a branch that exists, and the brief
    directory sits inside a git repository.
11. Dry-run check: `foreman front add <dir> --dry-run` exits 0. This is
    the completion criterion for the whole skill — a brief that "looks
    good" but was never dry-run is not done.

## 9. Handover

1. Write `brief.toml` and `plan.md` (your reasoning, short: the want, the
   task split and why, the open risks) into one directory.
2. Run `foreman front add <dir> --dry-run` yourself. On any refusal, fix
   the brief and re-run; hand over only on exit 0.
3. Hand over the directory path and the dry-run output. The owner's
   approval plus `front add` without `--dry-run` is the planner's last act.

