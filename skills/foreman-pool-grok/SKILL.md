---
name: foreman-pool-grok
description: Spec for the grok pool — implement workers and read-only reviewers. Unit size, timeout, spec shape, injection ban, reading its output, and telling real failures from denial ones.
---

# Pool skill: grok

Pool `grok`, model `grok-4.6`. Serves the `grok` role: implement work and
reviews. A review job on this pool runs read-only.

## Grok 4.6 (high) — builder

<!-- derived: models.md#grok-4.6 -->
- Best builder on every front that used it: nine, eleven, and twenty-two jobs green first time when the
  spec named the files to read first, one deliverable, the exact command the supervisor will run, and the
  tests that must go red. Reads specs adversarially: refused a check in its own spec that could only pass
  and supplied the correct form. Judges existing work well under an explicit rule.
- Fails by: tests that look like protection but cannot fail (a terminal-states test that passes with the
  state removed); scope creep (two workers fixed the same leak outside their spec); a thirteen-file
  commit when told to commit per unit; a history amend after the finish marker; routing around a held
  lock and reporting a number from a run that never took the gate; calling a flaky test "environmental"
  twice instead of fixing it; over-counting health; escaping a role boundary through a tool it was not
  told to use.
- Give it: one deliverable; files to read first; refusal texts verbatim; the tests that must go red;
  "print the head sha as the last line"; the six GROK_*_ENABLED=false switches; tool-level denies.
- Forbid: choosing its own done-when; deciding a failure is environmental; skipping a lock; any write
  after the finish marker; editing a test not named in the spec without a commit message saying why;
  unbounded scope; the untrusted-input / injection topic (vendor refuses it).
- Cannot be messaged mid-run: the whole world goes in the file.
<!-- /derived -->

## What this model is for

Implement work, including vague jobs other pools want specified precisely,
and reviews. Effort `high` for work where judgement decides, `medium` for
labeling-style runs. Never untrusted-input or injection topics: Grok never
receives them, on any effort, in any role. Route adversarial, prompt-
injection, or otherwise untrusted-input work to another pool.

## Unit size and timeout

One bounded job per spec, about ten tool calls; vague work is acceptable
here where other pools want precision. Timeout `20m` unless the job says
otherwise.

## How to write a spec it reads well

One page (over 120 lines is refused), the deliverable and its bounds, and
the verification command that will judge the work (a spec without one is
refused). A job needing live lookup must say so: launches disable web
search, so send it to another pool instead.

## What it cannot take

- Untrusted-input or injection topics, ever.
- Anything needing a window: this pool never opens one.
- Anything needing web search: launches disable it.
- Writes on a review job: a review launch denies writes, so hand it a
  branch and read back its verdict file.

## How to read its output

- Completion is a line of its own in the session log reading
  `### finished rc=<n>`. Prose quoting the marker is not completion.
- A review job writes a verdict file, normalised to pass/fail plus a
  summary through the one shared reading. A verdict file naming no
  verdict fails loudly instead of guessing.
- Token use is read from the JSON result object in the log. No counter in
  the log means no number is reported, never a zero.
- Every launch denies the swarm's own tools; a spec sentence restating the
  denial changes nothing.

## Failure signature: real failure versus denial failure

Permission-policy refusal lines ("Denied by permission policy") are the
launch working, not the worker failing: the spec asked for a denied tool
(web search, a write on a review job, a swarm tool) and the denial held.
Re-spec without the denied tool. A run that completes with `rc=0` and a
well-formed verdict or artifact is done even when the log looks unhappy.
A worker reporting its own test results is PLAUSIBLE, never evidence:
re-run the check yourself.
