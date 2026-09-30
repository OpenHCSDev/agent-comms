# Existing owners to extend

Extend the determining owners below and migrate their callers together.

| Fact or operation | Existing determining owner | Derived consumers and forbidden replicas |
| --- | --- | --- |
| enablement, reserve and recent-window settings | Selected Pi SettingsManager, `PiCompactionDecision` observation | S1 timing must not copy budget defaults or override hard limit |
| bounded packing, worker limit and retained-summary sizing | `stack/native-compaction-policy.mjs:CompactionPolicy` | S2/S3 extend one policy; PR416 owns output accounting |
| source revision, owner and ingress custody | `CompactionSource`, `CompactionBoundary`, native witness | Fact/boundary projections carry revisions; no independent freshness booleans |
| goal identity, revision, status and attempt | `Goal`, Registration and `GoalAttemptStore` | Memory is a derived read, never a second goal ledger |
| input identity and disposition | `OriginalTurnInput`, `InputDispositions`, native input proof | Retained text cannot mark UNKNOWN started or authorize replay |
| narrative source and prepared checkpoint | native SessionManager/entry store, SummarySource/HistorySummarySource | Retained facts join native preparation instead of being injected by a second prompt writer |
| selected provider exchange | `SelectedSummarySlot` and selected native stream | Cache capability belongs at the transport/route owner, not ACP |
| summary outcome, commit and original admission | `OwnerSummaryOutcome`, `NativeSummary`, `OwnerCompactionCommit` | Extend existing payload and outcome contracts; no separate memory commit |
| publication | TurnProgress/event family and existing compaction outbox | UI derives view; no memory authority in Toad |
| original peer message and claims | `Message.reference`, `WireLog.messages_for_references`, wire-owned `ClaimTransition` / `ClaimProjection` | Preserve original seq/ID, author, scope and wire evidence; no reconstructed transcript authority or independent claims/memory store |

## Shared Decision contract

| Abstraction | Builder | Users |
| --- | --- | --- |
| Decision | S2 | S4 |

S2 owns the nominal wire Decision and emission tool. S4 reads original Decision
records for revision mass and distinction probes. Native retention, packing,
commit and UI derive views from the same original wire record. Extend FieldCodec
and existing tool/message declarations; never subclass FieldCodec or add another
memory store, prose extractor, codec or checkpoint pipeline.

Message.reference carries seq/ID; WireLog provides certified bounded reads.
ClaimTransition lives on the original message; ClaimProjection derives current
claim ownership. Constraint applicability follows those original authority/scope
contracts. Sequence shared wire/tool/preparation changes under their existing
implementation owners.

## Correct-factoring experiment

Add one new fact kind, such as an exact artifact identity. Its owner should expose
its projection and invalidation contract once. Budget packing, checkpoint commit,
UI display and evaluator should consume the public contracts, not gain an
`if kind == ...` arm or a copied roster each. Independent fact sources need not
share a mutable aggregate or inheritance hierarchy just because fields look alike.

Add one provider route capability. That transport determines support and request
shape. Strategy selection consumes that capability; it cannot guess from a model
name in Python, JavaScript and Toad separately.

Check dependency binding, MRO, constructor effects and every consumer before
applying an ownership migration. Default to refusing unsupported bindings, rather
than creating aliases or parallel interpreters.
