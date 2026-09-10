# Supervising: how a supervisor spends its own turn

Source of truth for what a supervisor does between launching a job and reading its outcome. Skills derive
from this file; amend it with a dated line and the ruling id, never silently.

## Wait in the background, never in the foreground (owner, 2026-09-10)

`foreman wait` is the one way to wait for a job. Run it in the background. Two things are wrong and both
were done on 2026-09-10:

- **A foreground wait makes the supervisor unreachable.** The orbit-v2 supervisor blocked its session for
  up to ten minutes at a time; the owner could not reach it and interrupted it three times.
- **A hand-rolled polling loop is not the fix.** The same supervisor read "don't wait in the foreground" as
  "don't use `foreman wait` at all" and polled a session file in a loop instead. That is clumsy, it misses
  the outcome the runtime already knows, and it was a narrower lesson than the incident taught.

The rule: `foreman wait` in the background, so the supervisor keeps its turn and stays reachable while the
runtime watches the job. Never a foreground wait. Never a polling loop.

## Rules about who may run are enforced at launch, not remembered (owner ruling implied 2026-09-10)

The orbit-v2 supervisor broke the owner's standing rule — never Opus as a builder, never two Opus jobs at
once — while improvising under pressure after its Grok pool died mid-job. The ruling existed, recorded, in
its notes and on the board, and Foreman accepted the launch anyway.

A system that depends on the supervisor remembering is weakest exactly when the supervisor is busy. Every
rule of the form "this pool may not hold this role" or "at most N of this pool at once" belongs in the
launcher as a refusal with the rule quoted in the refusal text. A rule that is only written down is not
enforced.

## What holds, from the same session

- **Prove before claiming.** Every fix fails on the old code before it passes on the new. This caught weak
  tests more than once. See verification.md.
- **Accept on the real thing.** Each phase proven on the live cluster, not the fake one. On 2026-09-10 this
  found a dry run that really deleted; four hundred tests had missed it, because the fake behaved
  differently from the real one. Without the live run it would have shipped.
- **Written memory beats trust.** The ledger, evidence and findings let a supervisor pick up after a context
  reset without guessing, and let the owner check a claim instead of believing it.
- **A second session watching is worth its cost.** The foreman confirmed a pool was really out rather than a
  false alarm, and took a machine-wide defect off the supervisor's plate.
