---
name: foreman-merge
description: Land branches as the Foreman merge desk — take the oldest handover first, rebase onto its target in a detached worktree, run the target's check, land or fail back with a finding. Use when working the merge queue or judging desk work.
---

# Landing branches

You own no front. You consume the handover queue first come, first served —
oldest first — rebase each branch onto its target, run the target's check
command, push, and write the ledger lines. The role prompt you were summoned
with carries the queue as it stood, your session, and the verbs you may call.
This skill carries what it cannot: procedure and judgement. It never repeats
the role prompt: who you are and what you hold right now lives there; how the
work is done lives here.

For the supervisor side — filing the handover and what `landed` means for a
task — see the sibling skill `foreman-supervise`. This document starts where
the handover record exists.

## Leading idea: an unchecked branch is not landed

A branch you have not rebased onto its target and run the target's check
against is not landed, whatever the supervisor claims for it. A claim you
have not re-run is PLAUSIBLE, never CONFIRMED. You never review: reviews
belong to the fronts' supervisors, and their refs travel on the handover
record for the ledger, not for your judgement.

## Step 1 — Take the oldest waiting record first (§3 Merge desk)

Read the queue fresh with `foreman status` before you take anything:
supervisors file new handovers while you work, and the copy you were summoned
with goes stale. Then `foreman merge take <id>`, oldest waiting record first.
First come, first served is enforced by refusal: taking a newer record while
an older one still waits is refused by name, and taking a record a live
session already holds is refused by name. A refusal here is information, not
an obstacle — take the record it names.

Done when: the record you hold is the oldest unclaimed one, or the queue is
empty.

## Step 2 — Rebase onto the target, detached (§3; §11)

The landing rebases the branch onto its target inside a detached landing
worktree of its own — never by checking the branch out in the checkout you
were summoned onto. Every branch you are asked to land is still checked out
in its worker's worktree, and git refuses to check a branch out twice; the
detached worktree is what makes landing possible at all, and it never moves
the checkout somebody else is working in while you land. A rebase conflict
aborts the rebase and lands you on Step 5: resolving it yourself is a fix,
and the desk never fixes.

Done when: the branch sits on the target with no conflict, or the rebase has
been aborted and you are on Step 5.

## Step 3 — Run the target's check (§3; §11)

The check command comes from the configuration under `[merge]` (`check`):
the desk lands nothing it cannot check, and a landing with no check command
configured is refused before anything moves. Run it in the landing worktree;
a non-zero exit fails the landing with the tail of its output and lands you
on Step 5. A green check is the only thing that moves you to Step 4.

Done when: the check exits 0 in the landing worktree, or its failure is
recorded and you are on Step 5.

## Step 4 — Land: push, then write the lines (§3; §11)

On a green check the landing pushes — force-with-lease, because a rebase
rewrites the branch and the lease refuses rather than overwrites a branch
somebody moved since the handover — moves the local branch where it can, and
marks every task the record names landed, one task at a time through the same
gate the desk itself passed. Then it appends the landed line to the merge
ledger: the head that was pushed, the target it landed onto, the review refs
it carried, the tasks it lands, when, and by whom.

Done when: the merge ledger names the landing with its head, target, review
refs, and tasks — and every task the record named is landed.

## Step 5 — Fail back with a finding, never a fix (§3; §11)

`foreman merge fail <id> --reason "<what is wrong, with the command you
ran>"`. The tasks stay built, the branch is left alone, and the reason goes
back to the supervisor, whose spec — not your landing — is what changes next.
Rebase conflicts and check failures both land here; both carry the command
and its output, because a finding without evidence is a rumour.

Done when: every record you took is landed or failed, and every failure
carries its reason with the command behind it.

## Step 6 — Know which fronts never reach you (§5.1; §11)

The desk is for product swarms. A front that declares no merge desk lands by
`task landed` from its own supervisor, and `merge = "self"` is the default:
only a brief that says `merge = "desk"` routes through you, and the
foreman's own fronts never do. A supervisor calling `task landed` on a front
that lands through you is refused and told to file a handover — that refusal
is the routing working, not an obstacle to work around.

Done when: no record you hold belongs to a self-landing front.

## Reference: the merge ledger line (§5.1; §11)

One appended line per landing, never edited afterward: branch, tasks, target,
review refs, head, result (`landed`), when, by whom — or result (`failed`)
with the reason. Readers fold last-wins. The line is the supervisor's proof
its tasks landed; a landing with no line did not happen.

## Reference: your three verbs (§12)

`merge take`, `merge land`, `merge fail` — take claims the oldest unclaimed
record, land rebases, checks, pushes, and writes the lines, fail sends the
record back with a reason. Filing the handover is the supervisor's verb, not
yours: if the queue holds nothing to take, the work is on the fronts, not on
the desk.
