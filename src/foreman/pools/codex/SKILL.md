# Skill: `foreman-pool-codex`

How a supervisor writes a spec this pool does well, and how to verify
its output.

## What it is for

Reviews (`astra` reviewers). This pool exists for second reads, not
for building.

## How to spec for it

- Unit size: one review per spec: the branch, what was built, and the
  check that decides pass or fail.
- Effort: `high` or `medium`. There is no fourth level: `xhigh` runs
  as `high`.
- Timeout: 25 minutes unless the job says otherwise.
- Every spec names its verification command. A spec without one is
  refused.

## What it cannot take

- Implement work: hand it a branch to judge, not a job to do.
- Anything needing a window: this pool never opens one.
- A verdict path colliding with the agent's last-message file: the two
  are kept apart at launch, and a collision is refused loudly rather
  than letting the last message overwrite the verdict.

## How to read its output

- The spec reaches the reviewer on standard input; the session log
  carries the transcript.
- Completion is a line of its own in the session log reading
  `### finished rc=<n>`. Prose quoting the marker is not completion.
- The verdict file is normalised through the one shared reading, so
  this pool never disagrees with another about the same review.
- This pool exposes no token counter: usage is always reported as
  nothing, never an invented number.
- The vendor's own rollout transcript pointer is recorded beside the
  session for a person reading later; the collector never depends on
  it.
