# Paired recorded resource comparison

ScoredScenario now owns both per-arm usage projection and paired differences.
The existing single/batch comparison CLI consumes those results. All seven usage
metrics and five resource scopes share the same missing-data rules. A batch
compares aggregate totals; it does not average percentages or drop missing
trajectories. An observed zero remains zero, with relative reduction unavailable.

Final qualification uses bounded authored usage and the actual existing CLI
with two absent-record pairs. It exercises no native/model/original input.
The first batch passed eight tests and six subtests; the ninth test refused its
invalid authored SummaryUsage constructor. Required cache counters were supplied
and only that test reran successfully. The original negative is preserved.

Before/after AST outputs use existing audit.findings.Package over src/tests/tools:
734 parsed modules, zero omissions. Queries select relevant declaration and
consumer names, with new method names in the after view. Counts do not compare
different query sets and lexical resolution is not dynamic execution proof.
Original PiUsage/SummaryUsage and existing record readers are unchanged.

Recorded normalized cost is not billing. Complete raw sums do not establish
matched interventions, independent preparation, registered margins, cache
savings or whole-turn speed. Full S4 and the unapproved30-pair/USD75 study remain
unfinished. Raw CLI files and hashes are named in receipt.json; no repeated SDK,
original602/639/641 reads, provider calls, package, loan or environment.
