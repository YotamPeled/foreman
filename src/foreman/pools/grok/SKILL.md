# Skill: `foreman-pool-grok`

How a supervisor writes a spec this pool does well, and how to verify
its output.

## What it is for

Implement work and reviews. A review job on this pool runs read-only:
writes are denied, so hand it a branch and read back its verdict file.

## How to spec for it

- Unit size: one bounded job per spec, about ten tool calls. Vague
  work is acceptable here where other pools want precision.
- Effort: `high` for ordinary work, `medium` for labeling-style runs.
- Timeout: 20 minutes unless the job says otherwise.
- Every spec names its verification command (a `pytest`, `python -m`,
  `make check` or equivalent line). A spec without one is refused.

## What it cannot take

- Anything needing a window: this pool never opens one.
- Web search: launches disable it. A job needing live lookup must say
  so and go to another pool.

## How to read its output

- Completion is a line of its own in the session log reading
  `### finished rc=<n>`. Prose quoting the marker is not completion.
- A review job writes a verdict file; it is normalised to pass/fail
  plus a summary through the one shared reading, so this pool never
  disagrees with another about the same review. A verdict file naming
  no verdict fails loudly instead of guessing.
- Token use is read from the JSON result object in the log. No counter
  in the log means no number is reported, never a zero.
- Every launch denies the swarm's own tools, and a review launch
  additionally denies writes. These denials are the boundary; a spec
  sentence restating them changes nothing.
