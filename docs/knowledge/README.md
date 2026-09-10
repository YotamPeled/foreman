# Knowledge files

Source of truth for what the swarm has learned, written by the foreman from the owner's rulings and the
survey of every past foreman, supervisor and desk (docs/survey-2026-09-10/). Skills DERIVE from these
files and never restate them from memory: a per-pool skill quotes models.md for its pool, foreman-supervise
quotes team-calibration.md and verification.md, foreman-orchestrate quotes team-calibration.md's
assignment section. A test in the repository fails when a skill and its knowledge file disagree (decision
32: derived artifacts). Amend a knowledge file with a dated line and the ruling id, never silently.

- models.md — how each model behaves as a worker, reviewer or supervisor; what to give it; what to forbid.
- team-calibration.md — how the foreman derives a front's working team, and the signals that change it.
- verification.md — what counts as proof; red-then-green, the break, silence, evidence binding.
- supervising.md — how a supervisor spends its turn: waiting, and rules enforced at launch rather than remembered.
- rulings-2026-09-10.md — the owner's rulings from the survey, verbatim, with ledger ids.
