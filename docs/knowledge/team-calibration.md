# Team calibration (decision 31; rulings rul-egz7rm5, rul-egzdnag)

The owner's team line on a front is the ceiling. The foreman derives the WORKING team from the map and
the tree, records the derivation on the front, and adjusts it on the signals below. The owner is asked
only when the derivation needs more than the quota.

## The count
builders = min( independently verifiable leaves,
                the supervisor's verification rate,
                shared-resource slots )

- **Independently verifiable** is judged at verify time, not edit time: two leaves are parallel only if
  both worker pages can be written today without knowing either outcome, they touch disjoint modules
  (the map's module sizes say which are shared; on this runtime every friction touched launch.py), and
  neither needs the other's output to run its verify.
- **Verification rate**: one supervisor verifies serially. Observed: 5–15 minutes per job before v5,
  minutes after (the runtime does red-then-green and the break). Two builders per supervisor on one
  codebase was the ceiling on eight fronts; three or four only where leaves were disjoint by
  construction; six on a research front of independent lines.
- **Shared resources**: a fixture database, a lock, a temporary filesystem. Reserved with a count like
  model slots (decision 25). Past about three fixture-backed jobs the failures are about the machine, not
  the code.
- Repositories add landing lanes, not builders. Leaves resting on ASSUMED map facts run alone and first.
- A pair (two workers on one node) exists only for a structural reason: two independent readers whose
  disagreement is the product. Never a same-model pair for measurement (ruling rul-egymlfh).

## The model, from the shape of the node's verify
- Verify is a number, a diff, a golden file, an exact string → mechanical → cheapest builder that returns
  green first time (Muse high), no reviewer.
- Verify is a behaviour, a property, a test suite the worker also writes, a security boundary, a contract
  → strong builder (Grok 4.6 high; Opus 5 high as backup) plus an EXECUTING reviewer (Astra medium or Sol
  high) bound to the exact head.
- Specs, proofs, findings, adjudication stay with the supervisor.
- Anything whose done-when is "a throwaway session gets the right answer" → Fable.

## Reviews
- A verify that goes red beats a second read. Buy a reviewer only for behaviour and security nodes, and
  only one that executes; require network access or treat the round as a read.
- One node, one head, one verdict. Never bundle nodes into a review. After two rounds finding the same
  defect class the node returns to the supervisor for redesign (ruling rul-egyy6ar).
- Reserve reviewers phased: builders first, reviewers when a head exists (decision 32).

## Signals during a front
Add a builder when: ready independent leaves wait, the verify queue is empty, and the pool has room;
two consecutive leaves landed without conflict and the next two have disjoint must-not-touch lines.

Remove one when: a conflict was resolved by judgement (the tree lied about independence); two workers
touched one file; a returned job waited more than ten minutes for verification; three flakes in a row;
a pool refuses on the machine-wide count while the front's allocation is idle (the ceiling is elsewhere).

Change model UP when: the same leaf died twice on its limit; the failure is omission (a clean, plausible,
incomplete answer); the worker explains a failure instead of fixing it; the same defect class returned
twice from review; a worker's tests assert something already true (comprehension, not care).
Change model DOWN when: a review found nothing twice in a row; jobs return in under fifteen minutes.

Two fail-backs on the same seam mean the spec is wrong, not the worker: stop and re-investigate.
A mutation surviving on landed code means add a job, not a worker.
