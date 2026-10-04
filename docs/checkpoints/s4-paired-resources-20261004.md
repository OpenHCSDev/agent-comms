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
