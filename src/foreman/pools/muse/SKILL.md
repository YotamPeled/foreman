# Skill: `foreman-pool-muse`

How a supervisor writes a spec this pool does well, and how to verify
its output.

## What it is for

General implement work: collectors, labelers, fixtures, tests,
re-checks, doc and release commits, chart and config patches. This is
the default worker pool. It is not review-capable: it reads no verdict
file, so never give it a review job.

## How to spec for it

- Unit size: one bounded job per spec, about ten tool calls. One page:
  a spec over 120 lines is refused.
- Effort: `high` for ordinary work, `xhigh` for the hardest
  implementation. There is no `medium` here.
- Timeout: 20 minutes unless the job says otherwise.
- Every spec names its verification command (a `pytest`, `python -m`,
  `make check` or equivalent line). A spec without one is refused.

## What it cannot take

- Review jobs (no verdict reader).
- Anything needing a window: this pool never opens one.
- Pushes through the sandbox: the worker runs sandboxed with approvals
  off, reaches the network and local files, but authenticated remotes
  that live outside the sandbox stay unreachable. A job needing a push
  must say so.

## How to read its output

- Completion is a line of its own in the session log reading
  `### finished rc=<n>`. Prose quoting the marker is not completion.
- Token use is read from the JSON event stream in the log. No counter
  in the log means no number is reported, never a zero.
