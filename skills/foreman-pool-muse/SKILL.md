---
name: foreman-pool-muse
description: Spec for the muse pool — default implement workers. Unit size, timeout, spec shape, reading its output, and telling real failures from sandbox ones.
---

# Pool skill: muse

Pool `muse`, model `muse-spark-1.3-contributor`. Serves the `muse` worker
role: the default implement pool for everything that is not core logic.

## What this model is for

Collectors, labelers, fixtures, tests, re-checks, doc and release commits,
chart and config patches. First choice for bounded implement work; never
for review jobs (this pool reads no verdict file) and never for core-logic
engineering (that is the claude pool's `opus` role).

## Unit size and timeout

One bounded job per spec, about ten tool calls. Effort `high` for
mechanical work, `xhigh` for anything with judgement in it. Timeout `20m`
unless the job says otherwise.

## How to write a spec it reads well

One page (over 120 lines is refused), exact deliverable, the files that may
change and the files that must not be touched, and the verification command
that will judge the work (a spec without one is refused). Mechanical,
checkable wording; no architecture to decide.

## What it cannot take

- Review jobs: no verdict reader, so never a review kind.
- Anything needing a window: workers never open one.
- Pushes or logins through the sandbox: approvals are off but the sandbox
  stays on, so keyring-backed auth outside the sandbox is unreachable. A
  job needing a push must say so in the spec.

## How to read its output

- Completion is a line of its own in the session log reading
  `### finished rc=<n>`. Prose quoting the marker is not completion.
- Token use is read from the JSON event stream in the log. No counter in
  the log means no number is reported, never a zero.

## Failure signature: real failure versus sandbox failure

Retry lines (`retrying meta model stream`, attempt counters resetting) are
noise: the stream drops and backoff resumes, and runs have completed after
eleven silent minutes. Wait for the marker up to the timeout; never infer
liveness from the retry lines. A real death prints `agent loop failed` and
exits non-zero — relaunch rather than retrying into the same wall. Auth or
push failures ("not logged in", "unreachable") with `rc` otherwise clean
are sandbox denials: re-spec the job to avoid the outside credential
instead of failing the worker. A worker reporting its own test results is
PLAUSIBLE, never evidence: re-run the check yourself.
