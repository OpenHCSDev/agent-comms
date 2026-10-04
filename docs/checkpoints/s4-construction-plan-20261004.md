# S4 frozen construction sequence and prospective arm order

Source base: merged629/622 main `a40560fa`.

The existing scorer can read matched records, and `PairedRecallDesign` owns
the pinned oracle, selected conditions, model and sample count. It currently
has no runnable export for prospective construction. `RecallScenario.public`
exports cumulative snapshots; a caller would have to re-decide which history
is new at each cut and separately assemble the held-out prompt.

Extend the existing owners. `RecallRound` derives the new history from an exact
cumulative prefix; `RecallScenario` assembles ordered history/probe operands
once. `PairedRecallDesign` reads its pinned oracle and assigns prospective arm
order using an explicit sampling seed, distinct from its bootstrap seed.
The existing CLI exports that plan without launching anything. Preserve
Unicode/source text and omit oracle answer/evidence metadata from probes.

A construction plan is not an original input, a completed cut, a condition
intervention, preregistration, independent sampling, provider-capacity result
or spend approval. Full-context eligibility remains with original native
ContextBudget/source observations; no source text is truncated to make it fit.
The plan cannot retrospectively reconstruct historical submissions.

No new type/store/scanner/launcher, no model run, native edit, package or loan.
W1's native assembly and source spans remain Einstein's claim. The existing
configured native fixture continues to own execution/admission/commit/recovery.

Source reasoning and complete owner/caller implementation precede one focused
construction/CLI validation batch. Actual configured baseline interventions
and the separately unapproved 30-pair/USD75 study remain unfinished.
