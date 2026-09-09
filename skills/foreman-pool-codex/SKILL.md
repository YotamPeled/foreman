---
name: foreman-pool-codex
description: Spec for the codex pool — astra reviewers for second reads. Unit size, timeout, spec shape, reading verdicts, and telling real failures from harness ones.
---

# Pool skill: codex

Pool `codex`, model `gpt-6-astra`. Serves the `astra` reviewer role. This
pool exists for second reads, not for building.

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
