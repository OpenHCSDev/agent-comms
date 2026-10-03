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
