# Skill: `foreman-pool-claude`

How a supervisor writes a spec this pool does well, and how to verify
its output.

## What it is for

Core logic and cut-throat engineering (`opus` workers), front
supervisors, and reviews. The model is named per role: an `opus` worker
runs the Opus model, and no role ever runs on whatever a machine
happens to default to.

## How to spec for it

- Unit size: core-logic slices a Muse worker cannot carry alone.
  Precision over breadth: name the exact deliverable, the files that
  may change, and the files that must not be touched.
- Effort: this pool takes the launcher's effort through.
- Timeout: 20 minutes unless the job says otherwise.
- Every spec names its verification command (a `pytest`, `python -m`,
  `make check` or equivalent line). A spec without one is refused.

## What it cannot take

- Anything needing a window for a worker: workers never open one.
  (Supervisors are the interactive sessions; that is a different
  launch shape.)
- A launch that guesses: the model, the working directory and the
  permission mode are all named on the command line. A spec relying on
  machine configuration is a misspecification.

## How to read its output

- The spec reaches the worker on standard input; the session log is
  the JSON transcript.
- Completion is a line of its own in the session log reading
  `### finished rc=<n>`. Prose quoting the marker is not completion.
- Token use is the last usage object in the transcript. No usable
  usage object means no number is reported, never a zero.
- A review job writes a verdict file, normalised through the one
  shared reading like every other review-capable pool.
