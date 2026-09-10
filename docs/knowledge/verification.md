# Verification: what counts as proof (decisions 22, 23, 26, 33; rulings rul-egxrm5k, rul-egxvedy, rul-egxzbqb, rul-egy524w)

- **Only running is evidence.** A worker's green suite, the supervisor's green suite and a reviewer's
  green verdict are three claims about one thing. Every worst defect of v0 survived 202 passing tests.
- **Red then green, by the runtime.** A node's verify runs on the base (must fail) and on the head (must
  pass) in a fresh worktree before anyone reads the result. A verify never observed red is refused. The
  door refuses an EMPTY verify and never judges its vocabulary.
- **The break.** Every node names the one-line change that must turn its verify red; the runtime applies
  it after the green run, must see red, restores, records. A surviving break is classified by the
  supervisor before the node proceeds: coverage gap (add the assertion), ineffective mutation (note it),
  vacuous test (delete the test; a line that reads like protection and protects nothing costs the next
  person more than the bug).
- **A test of a check must record an identity.** Every backward-compatibility escape hatch is a hole in
  every test that does not deliberately fill it. Mutation-check every test whose subject is a check.
- **Silence is not a pass.** A check that stopped early, looked in the wrong place, or reported nothing;
  a reviewer killed by quota or an access flag; a negative result with no positive control: each is its
  own recorded state. Construct the condition, never wait for it. A check must say what it saw.
- **Evidence is bound to head plus base.** A target move re-runs the check and marks prior verdicts
  stale. Take the head at verify time, not at return time (a job amended its commit after returning).
- **Which build proves a proof.** The runtime commit that ran the proof is recorded on the evidence;
  evidence from a build other than the front's contracted checkout is not admissible.
- **A verify that does not exist or cannot run** is filed as an error finding and the node stops.
- **Name the property, not the cases.** A spec naming two error shapes found the fix in two and missed
  four. A verify that only proves the code ran is theatre: state what a wrong answer would look like.
- **Green is a count under contention.** A ledger test failing one run in three passed once for the
  worker and once for the supervisor and told nobody anything. If two runs on the same tree disagree,
  stop adding workers and take a paired baseline: you are measuring the machine.
- **Agreement is evidence only when the reasons agree.** Two readers confirmed 58 wrong rows; two
  silences agreed about one app for different reasons. Record the reason beside every count.
- **A note explaining a hazard inside the code that has it reads as handled.** Write the guard, not the
  warning; make must-not-touch executable and adjacent to the act.
- **Check what your check can see** before trusting what it says: a grep on one field, a diff cut by
  head, an exit code off the end of a pipe, a declaration true of dev that became false when the service
  landed.
- **Report the thing that makes you look bad, by name, immediately.** It is the only reason a ledger can
  be trusted.
