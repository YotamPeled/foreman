---
name: foreman-pool-codex
description: Spec for the codex pool — astra reviewers for second reads. Unit size, timeout, spec shape, reading verdicts, and telling real failures from harness ones.
---

# Pool skill: codex

Pool `codex`, model `gpt-6-astra`. Serves the `astra` reviewer role. This
pool exists for second reads, not for building.

## Astra 6 (gpt-6-astra, low/medium) — executing reviewer; contract adjudicator

<!-- derived: models.md#astra-6 -->
- Highest-value reviewer when it executes: built a fake vendor and drove the launcher, found P1s green
  tests hid (three in deploy tools, four plus five in viewer tools), found the role prompt never reached
  the worker. Reports honestly what it could not run ("reasoned, not reproduced"). States preconditions
  well in briefs.
- Fails by: its sandbox silently denying sockets so "judge by running" becomes reading for rounds;
  40+ minutes per review; drifting into unrelated documents; proposing a redesign of settled design;
  reaching for a board or tool it was not given (six times unprompted); printing its verdict twice;
  verdict vocabulary drift (APPROVE for MERGE); dying on access flags with no verdict; as a supervisor,
  ignoring queued messages, launching off-roster, launching after a freeze.
- Give it: exact candidate and base sha; the invariants; executable counterexamples; "do not propose a
  redesign of anything the design already settles"; network access or treat the round as a read; the
  gate vocabulary.
- Forbid: the bypass-sandbox flag (silently resolves to full access); supervising; any MCP it was not
  given; counting an interrupted review as a verdict.
<!-- /derived -->

## What this model is for

Reviews: hand it a branch to judge, with the check that decides pass or
fail. Never implement work, never a job to do.

## Unit size and timeout

One review per spec: the branch, what was built, and the check that
decides pass or fail. Effort `high` or `medium`; there is no fourth level,
so `xhigh` runs as `high`. Timeout `25m` unless the job says otherwise.

## How to write a spec it reads well

One page (over 120 lines is refused), the branch and the pass/fail check
first, and the verification command that will judge the review itself (a
spec without one is refused). A judgement, not a task: ask "does this
branch satisfy …", never "build …".

## What it cannot take

- Implement work of any kind.
- Anything needing a window: this pool never opens one.
- A verdict path colliding with the agent's last-message file: the two are
  kept apart at launch, and a collision is refused loudly rather than
  letting the last message overwrite the verdict.

## How to read its output

- The spec reaches the reviewer on standard input; the session log carries
  the transcript.
- Completion is a line of its own in the session log reading
  `### finished rc=<n>`. Prose quoting the marker is not completion.
- The verdict file is normalised through the one shared reading, so this
  pool never disagrees with another about the same review.
- This pool exposes no token counter: usage is always reported as nothing,
  never an invented number.
- The transcript pointer the launch records beside the session is for a
  person reading later; never depend on it for state.

## Failure signature: real failure versus harness failure

A missing or prose-shaped verdict file is a harness failure (last message
overwrote the verdict, schema mismatch), not a failed review: fix the
paths and re-dispatch. A well-formed verdict that fails is a finding about
the branch, and returns the task to active. A reviewer reporting its own
summary of the branch is PLAUSIBLE, never evidence: the verdict file plus
your own re-run of the check is what decides.
