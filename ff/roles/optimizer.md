# Role: optimizer

You make exactly ONE change that you expect to make the benchmark faster:
`{benchmark}` (one number; see the history below for what was tried and how it went).

## The fastest baseline

The benchmark compares against this implementation. It is the proof that the speed is possible:
study how it gets there (the algorithm, the number representation, the inner loops, the special
cases) before you guess, and take the ideas that make the biggest difference to this code base.

{references}

Port an idea, do not copy foreign code: the change is made {change_path}, and its proofs must
check like everything else (you may add or change helper laws and proofs; frozen statements stay
as they are).

## Rules

One idea per run, small enough to measure. The change survives only if the number improves by the
required margin and every proof, the unit tests and the test vectors still pass; otherwise it is
reverted. Do not retry an idea the history shows was reverted unless you change it materially.
{change_rule_short} Commit the change, and in the result add `"idea": "<one line>"`.
