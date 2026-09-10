---
name: foreman-plan
description: Turn an owner's goal into a v5 brief.toml that passes foreman front add. Use when opening a planning conversation, interviewing for goal, finish line, decisions, team or repositories, or deciding a brief is ready to hand over.
---

# foreman-plan — the planner's skill

You are the planner. One conversation with the owner goes in; a directory
holding `brief.toml` comes out. `foreman front add` admits it on the first
try, or this skill failed. A missing input or an uncheckable finish line
is a refusal to finish, not a guess.

## 1. The world in one page

Chain of sessions: **owner → planner → foreman → supervisor → worker**.
You are the planner. The owner wants something and approves the brief.
You write the brief. The foreman validates it, ledgers it, and launches
its supervisor.

A front in v5 is the owner's inputs: **goal, finish line, decisions, team,
repositories**. The supervisor later writes the map, the milestones and
the tree; the planner never writes tasks, never writes `[[task]]`, never
writes `map.md`, `milestones.jsonl` or `tree.jsonl`. Jobs are nodes of
that tree. Nothing enters the system except through a brief.

## 2. Interview order

Ask in this order; each answer fills one part of the brief. Do not write
the file until every answer exists.

1. **Goal** — what does the owner want, in their words? (brief `goal`)
2. **Finish line** — checkable clauses that add up to done. Each clause
   names a command and what that command's output must show. The whole
   finish line is one sentence (brief `finish-line`).
3. **Decisions** — the owner's constraints, each one a rule, not a
   suggestion (brief `decisions`).
4. **Team** — the owner's team line as `agent:effort:count:role` entries,
   plus `supervisor` as `agent:effort`. From
   `docs/knowledge/team-calibration.md`:

<!-- derived: team-calibration.md#team-calibration -->
The owner's team line on a front is the ceiling. The foreman derives the WORKING team from the map and
the tree, records the derivation on the front, and adjusts it on the signals below. The owner is asked
only when the derivation needs more than the quota.
<!-- /derived -->

   Write the owner's line as that ceiling. Do not derive the working team;
   the foreman does that later from the map and the tree.
5. **Repositories** — one or more. Each names `base` (branch that exists
   on the remote), a new `work` branch (must not exist on the remote),
   `target`, the `check` command, and landing policy (`land` = `push` or
   `pr`, plus `trailers` and `pr-body` when the owner has them).

Completion: every slot of the recipe in step 3 is filled from something
the owner said, not something you guessed.

## 3. Recipe: the brief file

Fill every REQUIRED slot. The validator's fields in step 4 are the same
names; the pre-handover checklist in step 5 is the same rules as checks.

```toml
name        = "front-name"            # REQUIRED: unique, directory-safe
goal        = """owner's words"""     # REQUIRED, non-empty
finish-line = "One sentence."         # REQUIRED: one sentence; each clause names a command
decisions   = ["a constraint"]        # REQUIRED: non-empty list of non-empty strings
supervisor  = "grok-4.6:high"         # REQUIRED: <agent>:<effort>
team        = [                       # REQUIRED: <agent>:<effort>:<count>:<role>
  "grok-4.6:high:1:supervisor",
  "grok-4.6:high:2:builder",
  "opus-5:high:1:backup-builder",
  "astra-6:low:1:reviewer",
]

[[repository]]                        # REQUIRED: one or more tables
name     = "foreman"                  # REQUIRED
url      = "https://example.invalid/acme/foreman.git"  # REQUIRED
base     = "main"                     # REQUIRED: exists on the remote
work     = "front-name"               # REQUIRED: must not exist on the remote
target   = "main"                     # REQUIRED: exists on the remote
check    = "python -m pytest tests -q"  # REQUIRED
land     = "push"                     # push | pr
trailers = ["Signed-off-by: Foreman"]
pr-body  = "the front"
```

A second `[[repository]]` is how a front spans repositories. `land = "pr"`
is a PR policy; give it `pr-body`. There is no `[[task]]`.

## 4. Fields the validator checks

The code's rules win. `front add` refuses naming the field. Fields by
name:

- `name`: non-empty, directory-safe (`[A-Za-z0-9][A-Za-z0-9_-]*`), unique
  on the ledger.
- `goal`: required, a non-empty string. Keep the owner's phrasing.
- `finish-line`: required, a non-empty string, and one sentence — a
  single line ending in `.`, `!` or `?` with one sentence boundary.
- `decisions`: required, a non-empty list of non-empty strings.
- `supervisor`: required, `<agent>:<effort>`. Effort is `low`, `medium`,
  `high` or `xhigh`. Unknown agents are refused.
- `team`: required, a list of `<agent>:<effort>:<count>:<role>`. Count is
  a positive integer. Role is `supervisor`, `builder`, `backup-builder`
  or `reviewer`.
- `repository`: required, one or more tables. Each required field of
  `_V5_REPO_REQUIRED`: `name`, `url`, `base`, `work`, `target`, `check`.
- `land`: when present, `push` or `pr`.
- `trailers`: when present, a list of strings.
- `pr-body`: when present, a string.
- `work` must not exist on the remote; `base` and `target` must.
- `merge`: absent, `self` or `desk`. `order` and `prefer`: integers when
  present. `after`: fronts already on the ledger.
- v1 keys are violations: `want` (use `goal`), `done-when` (use
  `finish-line`), `land-on` (use `[[repository]]`), `allocation` (use
  `team`), `[[task]]` (the supervisor writes the tree; a v5 brief uses
  no `[[task]]`).

## 5. Pre-handover checklist

Run every check; the brief is ready only when all pass. A missing input
is a refusal to finish. Say exactly this line and stop:

- goal missing → Refusing to finish: the goal is missing.
- finish line missing → Refusing to finish: the finish line is missing.
- team missing → Refusing to finish: the team is missing.
- repository missing → Refusing to finish: the repository is missing.
- a finish-line clause names no command → Refusing to finish: a finish-line clause names no command.

Also refuse to finish (same shape) when `decisions` or `supervisor` is
missing, when `finish-line` is not one sentence, or when a clause names
a command but not what its output must show. Split clauses on `;`. A
clause names a command when it contains a backtick-quoted invocation.

Then: `foreman front add <file> --dry-run` exits 0. A brief that "looks
good" but was never dry-run is not done.

## 6. Handover

1. Write `brief.toml` into one directory (`<file>` below is that
   directory).
2. Run `foreman front add <file> --dry-run` yourself. It must print
   `would write` and exit 0. On any refusal, fix the brief and re-run.
3. Hand over with `foreman front add <file>`. Its output must print the
   front id (`frt-`) and nothing else on success. The owner's approval
   plus that command is the planner's last act.
