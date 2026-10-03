# S4 original request construction

Arendt owns this continuation in the same reused checkout. PR570 and PR572 are
merged. Their original captures, seven hashes and two Started inputs remain
protected. The matched thirty-pair study is not authorized.

## Existing source owners

`SessionContext` selects the SDK source and applies native compaction admission.
`TurnContext.capture` observes the actual assembled context and its original
native generation/digest; it does not select a new experimental history.
`ContextBudget` calculates source/final-request input and allowance from the
actual selected model. `ContextBudgetRequest` owns pre-stream decreasing
allowance negotiation. `NativeRequestObservation` owns request correlation and
transport/callback observations. Provider adapters run payload hooks before
final transformed-input admission. A hook's payload is therefore not proof of
the admitted or dispatched body.

The original observer presently captures SDK segments and route/prefix hashes.
It does not retain the model capacity, final budget or final HTTP body.
Completion selection and captured registry configuration answer different
questions. None supplies those missing historical request facts.

## Intended change

Reuse the existing native/context/request owners and observer. Capture their
actual selected source and post-transform admission relation, with request
correlation supplied by the original request owner. The scorer must consume
that relation rather than calculate another budget, infer policy from an enum
label, or replace original evidence with today's registry/model catalog.
Condition construction must execute the existing source selection mechanism;
labels alone remain unqualified. No additional store, lifecycle state or native
policy is proposed.

Native seams were sent directly to Mendel and Sch before implementation.
Sch reports no active changes to these owners; the native915 artifact stays
frozen. Source reasoning and the complete caller change precede final checks.
There is no new provider call or artifact build in this draft.

## Working source checkpoint

`e88f332d` implements the budget part of the relationship. The existing
`ContextBudget.admit` reports its actual input estimate, selected model and
capacity, available tokens, requested allowance and admitted allowance through
the existing request observer. Each of the nine provider admission paths passes
its original options. Pre-stream completion negotiation reports its revised
allowance through the same budget owner. It still cannot replay an accepted
stream; admission arithmetic, provider rejection rules and limits are unchanged.

`ContextBudgetRequest.model` is deleted. Its rejection consumer reads the
original `budget.model`; the AST shows two independent model assignments become
one. Cancellation derives from the request's original options; the constructor,
generated provider, observation patch and cancellation control are migrated
together. No scalar compatibility path is retained.

`RequestObservationPoint` uses the existing SDK Model declaration. Python's
existing `ReportedModel` and `RequestProgress` decode the external metadata
once. Optional observations remain absent for other stages and historical
records; they do not acquire model selection, admission or replay authority.
No observer reads a catalog or calculates an alternative allowance.

The existing TypeScript parser read the five native owner/declaration files at
the original and changed heads without omissions. The native template/admission
sites were read separately; that parser receipt does not claim to resolve
patch-generated or dynamic calls. Python production/test/tool roots were read
through the existing NRA package parser. Relevant consumers are native event
decoding, tracked and summary request observers, canonical diagnostic publication
and the measurement probes; their existing observation lifecycle is preserved.

[Native source owners](../../evidence/original-request-construction-20261003/native-source-owners.json)

## Still open; not Ready

Budget records use the original request ID. The exact SDK context's native
generation/digest still needs to cross the original context-publication boundary;
input ID alone is insufficient. `onContextReady` currently has a void return
contract, while the native publisher separately emits the original manifest.
That relation must be closed through those owners before the scorer can bind a
budget to an original manifest. No historical field or guessed join is added.

Condition construction still needs an actual original source selection through
`SessionContext`/native execution. These new budget fields neither create that
selection nor establish full-context eligibility or exact final HTTP bytes.
This is a published source checkpoint, not installed acceptance. No native
artifact, environment, provider call or original input was created or changed.
Final checks follow the complete related owner/caller implementation.
