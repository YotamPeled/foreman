---
name: foreman-pool-claude
description: Spec for the claude pool — opus core-logic workers and supervisors. Unit size, timeout, spec shape, reading its output, and telling real failures from environment ones.
---

# Pool skill: claude

Pool `claude`, model `claude-opus-5`. Serves the `opus` worker role (core
logic and cut-throat engineering), plus `supervisor` and `foreman`
sessions. The only pool that takes core-logic work.

## Opus 5 (high) — supervisor, backup builder

<!-- derived: models.md#opus-5 -->
- Verifies by running and keeps honest ledgers; caught silent ruling reversions by mutation; self-reports
  its own mistakes unprompted (wrote into the owner's live state directory once, said so, fixed it);
  declines to invent a check that does not exist.
- Fails by: confident wrong facts (a six-path hazard that was unreachable; wrong line references;
  invented timestamps 07:20Z written at 06:18Z; a cause named from a process name and a load average);
  `git add -A` in a worker's tree sweeping job files into a public branch; `git checkout <file>`
  destroying a worker's uncommitted edits; filing a finding whose root cause names a component never
  executed; raising an owner question on a decision the rules covered (hours lost); long silent
  orientation that reads as death; landing after one review instead of reviewing the fixes; judging a
  fix to a check by reading the diff.
- Forbid: typing any number, timestamp or line reference not pasted from a command; naming a cause
  without reading the command line; `git add -A` anywhere a worker worked; implementing except after two
  worker deaths.
<!-- /derived -->

## What this model is for

Core-logic slices a default worker cannot carry alone, precision
engineering, and front supervisors. Precision over breadth: this is the
pool for work where judgement decides, never for bulk mechanical jobs that
belong on the muse pool, and never for second reads that belong on a
reviewer pool.

## Unit size and timeout

Core-logic slices: one exact deliverable per spec, with the files that may
change and the files that must not be touched named. Effort `high`,
`xhigh` for the hardest implementation. Timeout `20m` unless the job says
otherwise.

## How to write a spec it reads well

One page (over 120 lines is refused), the exact deliverable first, the
verification command that will judge the work (a spec without one is
refused). Name the model, the working directory, and the permission mode
nowhere by assumption: everything the run depends on must be on the
command line or in the spec, never in machine configuration.

## What it cannot take

- Anything needing a window for a worker: workers never open one
  (supervisors are the interactive sessions; that is a different launch
  shape).
- Bulk implement or review-mill work: that spends the scarcest pool on
  what cheaper pools do as well.

## How to read its output

- The spec reaches the worker on standard input; the session log is the
  JSON transcript.
- Completion is a line of its own in the session log reading
  `### finished rc=<n>`. Prose quoting the marker is not completion.
- Token use is the last usage object in the transcript. No usable usage
  object means no number is reported, never a zero.
- A review job writes a verdict file, normalised through the one shared
  reading like every other review-capable pool.

## Failure signature: real failure versus environment failure

A launch that guesses at machine configuration (model default, working
directory, permission mode) fails before the work starts: that is a
misspecification, re-spec and relaunch. A run that completes with `rc=0`
but whose transcript shows no usable usage object is still a completed
run — report no number, never a zero. A worker reporting its own test
results is PLAUSIBLE, never evidence: re-run the check yourself.
