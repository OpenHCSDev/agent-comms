# Existing owners to extend

No new shared abstraction is built by this planning PR. Extend the owners below
only after admitting each surface's required answer and proving its callers.
New names in the historical plan are sketches, not a mandate to add classes.

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

## Newly requested provenance obligations

[S2](S2-MEMORY.md) now requires four retention classes and a declared Decision
owner. No Decision abstraction or storage is built by this planning PR. Existing
message/wire and claim authorities must determine authored constraints; native
transcript injection is only a representation. At refreshed main `4295d680`,
`Message.reference` carries seq/ID and WireLog exposes certified bounded original
reference reads. Claims/releases reside in original message ClaimTransition rows;
ClaimProjection derives their current state, not a separately writable claims
store. The retention/constraint-admission join still needs implementation admission.

Audit existing richer decision authorities before introducing the proposed nominal
Decision. If none supplies choice, valid rejected alternatives, scope, source turn
and explicit correction lineage, add that fact family once under the existing
durable ownership mechanism. Retention, summary packing/commit, UI and S4 metrics
consume derived projections. Do not duplicate the Decision in a memory store,
retrospective prose extractor, FieldCodec subclass, codec or second checkpoint.

S2 is the receiving design surface; S4 owns measurement only. Wire/tool admission,
source joining and native checkpoint files remain sequenced crossings with their
existing implementation owners, not new delegated review tasks.

## Correct-factoring experiment

Add one new fact kind, such as an exact artifact identity. Its owner should expose
its projection and invalidation contract once. Budget packing, checkpoint commit,
UI display and evaluator should consume the public contracts, not gain an
`if kind == ...` arm or a copied roster each. Independent fact sources need not
share a mutable aggregate or inheritance hierarchy just because fields look alike.

Add one provider route capability. That transport determines support and request
shape. Strategy selection consumes that capability; it cannot guess from a model
name in Python, JavaScript and Toad separately.

These are provisional desired maintenance relations. Dependency/MRO/effect and
all-consumer census are OPEN until production design admission. The bounded NRA
class census in [04-EVIDENCE.md](04-EVIDENCE.md) is discovery evidence only.
