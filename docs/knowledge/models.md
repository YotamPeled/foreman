# Models: observed behaviour (20 sessions, 2026-09-02 to 2026-09-10)

Every line here comes from an incident a session named. "Forbid" lines are rules for the spec or the
launch, not advice.

## Grok 4.6 (high) — builder
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

## Muse (muse-spark-1.3-contributor, xhigh ALWAYS — owner ruling rul-frzifag 2026-09-10; never high) — builder for mechanical leaves
- Honest self-reports on every claim checked across five fronts; cheap; obeys "do not write a summary,
  do not add a README, do not commit"; puts scratch in /tmp when told; censused 4,223 files correctly;
  found real harms with file-and-line proof; when handed an impossible check it said so instead of faking
  a result.
- Fails by: agreeing (confirmed 58 of 58 rows on the wrong trees; approved a wrong premise after
  executing examples); tests that cannot fail (five collector tests asserted what was already true, one
  derived its expectation from its own output); unstated invariants (compared two clocks, both directions
  wrong); dying on long runs about half the time (`model stream idle timeout`, and a healthy run looks
  dead: the retry counter resets); writing `[REDACTED]` over Bearer-shaped text into code and test
  titles; calling two probe scripts "two review rounds".
- Give it: a one-page contractor's brief: what exists today in two sentences, files it may write, files
  it must not touch, constraints as absolutes, the exact commands used to judge it, what not to produce;
  units small enough that a death costs one unit; work idempotent and relaunchable; a stub in front of
  anything shared it must not reach.
- Forbid: being the sole author of a node's only test (require a named failure mode per test, written by
  another model or the supervisor); critical-path work; auth-shaped prose; anything it cannot be made to
  quote verbatim from disk; self-certifying review history or merge authority.

## Opus 5 (high) — supervisor, backup builder
- Verifies by running and keeps honest ledgers; caught silent ruling reversions by mutation; self-reports
  its own mistakes unprompted (wrote into the owner's live state directory once, said so, fixed it);
  declines to invent a check that does not exist.
- Fails by: confident wrong facts (a six-path hazard that was unreachable; wrong line references;
  invented timestamps 07:20Z written at 06:18Z; a cause named from a process name and a load average);
  `git add -A` in a worker's tree sweeping job files into a public branch; `git checkout <file>`
  destroying a worker's uncommitted edits; filing a finding whose root cause names a component never
  executed; raising an owner question on a decision the rules covered (hours lost); long silent
  orientation that reads as death; landing after one review instead of reviewing the fixes; judging a
  fix to a check by reading the diff.
- Forbid: typing any number, timestamp or line reference not pasted from a command; naming a cause
  without reading the command line; `git add -A` anywhere a worker worked; implementing except after two
  worker deaths.

## Astra 6 (gpt-6-astra, low/medium) — executing reviewer; contract adjudicator
- Highest-value reviewer when it executes: built a fake vendor and drove the launcher, found P1s green
  tests hid (three in deploy tools, four plus five in viewer tools), found the role prompt never reached
  the worker. Reports honestly what it could not run ("reasoned, not reproduced"). States preconditions
  well in briefs.
- Fails by: its sandbox silently denying sockets so "judge by running" becomes reading for rounds;
  40+ minutes per review; drifting into unrelated documents; proposing a redesign of settled design;
  reaching for a board or tool it was not given (six times unprompted); printing its verdict twice;
  verdict vocabulary drift (APPROVE for MERGE); dying on access flags with no verdict; as a supervisor,
  ignoring queued messages, launching off-roster, launching after a freeze.
- Give it: exact candidate and base sha; the invariants; executable counterexamples; "do not propose a
  redesign of anything the design already settles"; network access or treat the round as a read; the
  gate vocabulary.
- Forbid: the bypass-sandbox flag (silently resolves to full access); supervising; any MCP it was not
  given; counting an interrupted review as a verdict.

## Sol (GPT-5.6, high) — executing reviewer
- Strongest executing reviewer on the boxes-era fronts: proved seven contract claims wrong by running
  them; reproduced restricted-host failures against previous source; caught a related case after an
  earlier correction, forcing a redesign rather than a patch.
- Fails by: over-reporting (coverage gaps already ruled out of scope); malformed polling; exceeding the
  requested update interval; APPROVE instead of the gate word; access-flag terminations with no verdict.
- Forbid: free-form verdict synonyms; claims broader than the executed assertions; treating its finding
  count as severity.

## Fable 5.1 (high) — RETIRED FROM THE ROSTER 2026-09-10 (owner: quota out; Opus 5 high takes its place as foreman, supervisor and judgement worker). Kept for the record.
- Judgement, tool surfaces, skills, acceptance; corrections were the highest-value input other sessions
  named (caught invented timestamps, a merge never announced, an exit-status assertion satisfied by
  unrelated refusals). Specs it wrote were built green first time by Grok.
- Fails by: long silent reading (split reads across explorers and checkpoint between); chaining a check
  and a launch in one shell line; a proof mutation on a fixture line instead of the clause.
- Forbid: mechanical work.

## Sonnet — interim reviewer, labeller
- One review traced a generator helper and ran all 24 tests correctly; its first attempt died on quota
  with no verdict. Labels only otherwise. Forbid: counting a quota death as a verdict.

## Every model
- A worker's report is a claim, never evidence. Three green claims about one thing are still claims.
- No model writes a timestamp or a number it did not paste.
- A quota or access failure is "unavailable", never a pass and never a fail.
