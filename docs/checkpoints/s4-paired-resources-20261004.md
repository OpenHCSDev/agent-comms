# S4 paired recorded resources

The original scorer totals summary, recall and source-input usage against frozen
round membership. Its comparison entrypoints currently compare recall only;
each consumer would have to separately decide which resource groups are complete,
how to handle absent usage and whether a zero-cost baseline admits a ratio.

Keep this behavior in existing ScoredScenario. One original usage projection and
one frozen-round total algorithm serve the arm and paired readers. Single-pair
and batch entrypoints derive differences from those totals, retaining both arms
and unavailable observations. Missing work cannot become a cheap zero; a real
zero baseline has a valid absolute difference but no relative improvement.

No runtime/native/tool change, new class/store, provider input, original-record
rerun, package/environment or study. Recorded SDK-normalized cost remains
separate from billing. Summaries and shared source preparation remain separate
scopes; complete sums do not prove independent arms. No end-to-end clock or
study margin is inferred from token/cost observations. The 30-pair/USD75 study
remains unapproved.

Read existing owners and AST consumers first; migrate both comparison entrypoints
in one batch. Final validation checks changed arithmetic and the existing CLI
with bounded authored usage records only, not model recall.

## Scoped Ready

The working family is published: existing ScoredScenario.resource_metrics owns
all seven original usage projections. recorded_resources and both comparison
entrypoints consume it. resource_difference owns absolute/relative arithmetic;
paired_resource_batch consumes that same behavior after totaling complete
trajectories. The seven inline per-arm projections are deleted in favor of the
shared projection (8 lines removed). No new class, scanner or store.

Final relevant sanity batch: eight tests and six subtests passed; one authored
SummaryUsage constructor omitted required cache fields. That negative is kept.
After correcting only the constructor, the affected test passed. The actual
existing --recorded-pairs CLI completed with two absent-record pairs: all five
resource groups retain two expected trajectories and unavailable totals/ratios;
empty stderr, no original files or provider/native processes accessed.

Evidence: evidence/s4-paired-resources-20261004/. Source AST covers734 Python
modules without omissions. This is scorer/recorded-resource scope only. No
installed/live runtime, billed cost, provider capacity, end-to-end timing or
comparative acceptance is claimed. Automatic Debt must pass at the exact freeze.
