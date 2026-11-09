# P23 remaining provider plan — simulation only

Authority: comms-friction's messages ca35d2830d39, 523ba958cb42 and f1881756614f. This artifact-only directory is authorized in the existing P23 allocation. The ownership evidence is the retained 18-provider map at `/home/ts/.cache/agent-scratch/p23-message-contract-trace/remaining-provider-map-977df935.md`, read together with comms-teammate's independent 17-provider map. Do not count that source map as runtime verification.

## Scope and current state

`provider_plan.py` authors `plan.json` against the actual current source and calls NRA's **public** `nominal_refactor_advisor.codemod` frontend to simulate it. It has **no apply path**. No checkout, environment, product-source edit, test, profile, commit, merge or delivery claim is authorized here.

The retained initial complete src+tests attempt is `initial-full-context.*`. It indexed 1,381 source files and exited 1 on an authored physical edit conflict: an ensure_import insertion overlapped a whole-module patch in relationships.py. This was not a runtime/product failure or a timeout. The imports and patches now occupy different projected stages.

The supported simulation completed at Core HEAD `3edfb8c6ed2b2d01c85c85d2f379dc71e19aa4d8`: **exit 0; is_clean=True; 26 operations, 2 stages, 49 indexed input files, 15 projected changed files, applied=False, zero concurrent source changes**. Original field assignments and original provider method ASTs remain unchanged in the declared preservation corpus, including original Todo.order/pending and FoldedProblemOrder. The only added provider method is Todo.goal_instructions. Protected goal_mutation.py and execution/turn_context.py bytes are unchanged; Generation retains GoalGenerationRead and LedgerAttempt's existing parent edit is untouched. See provider-surface-preservation.json. This is declaration-source preservation, not executed decoder/concreteness evidence.

The later simulation is bounded to every selected contract/provider module plus explicit producer, consumer, inheritance and protected inputs declared in `provider_plan.py`. It includes the original `goal_mutation.py` and `execution/turn_context.py` as unchanged protected inputs, and the existing consumer `tests/test_goal_runtime_annotations.py`. This is not a complete repository scan, global absence proof, metaclass execution or behavior proof. The original broad attempt and complete provider maps are retained rather than relabeled as passing evidence.

Read `simulation-supported.log`, its exit file, `simulation.json`, `summary.json` and `projected.diff` for the supported result. The attempted catalog extraction remains separately retained as `catalog-extraction-refused-plan.json`, `catalog-extraction-refused.log` and its exit file. Empty architecture guard arrays certify no global architecture. A successful source simulation does not execute class bodies, C3, dataclasses, imports or decoder fields.

## Admitted ownership and the supported two-stage trajectory

1. Import the existing nominal read contracts at their real providers.
2. Replace the structural Protocols by state-free ABCs, with ordinary annotated attributes for reads backed by original dataclass fields/__init__ assignments. Add empty slots to these capabilities. Bind the actual providers without moving their original fields or existing implementation methods.
The proposed third stage — moving the original RelationshipCatalogSource below both services with NRA's extract_symbols_to_new_module — was **refused**. It is absent from supported plan.json. Its original declaration/provider pair remains unchanged and OPEN. ChannelManagement does not import relationships merely to obtain a parent. See catalog-refusal.md for the exact causal branch and the minimal native reproducer/control. The implementation owner will route the lexical-scope fix and rerun the original move; this plan does not replace it with copied source.

The 18-role census is fully accounted for as **15 nominal read contracts, two deleted overpromises/mirrors, and one unchanged OPEN catalog contract**. Do not count the deferred catalog pair as repaired:

* `RelationshipBusSource` is deleted. Existing **BusResources** already owns the actual Bus/read resources and the one opened_activity implementation. RelationshipSource.bus names that original nominal owner.
* `GoalAssignmentOrder` is deleted. It promised instructions on a ProblemOrder even for the valid FoldedProblemOrder, which has no such method. There is no fake default and no new order record.

The real class providers remain ActivityClocks; BusResources -> PublishedBus -> MessageBus; HistoryViews; ChannelManagement; Channel; ChannelView; GoalDeclaration; Thread; Todo; TodoStore; goal_ledger.AttemptRecord; and Generation. Their frozen fields, wire annotations, defaults, SQL declarations and producer bodies remain at their original owners. Generation retains the implementation owner's existing GoalGenerationRead base; LedgerAttempt and TurnContext are not changed by this plan.

These are instance read interfaces. Display order **values** remain the original type[ThreadSort]/type[ChannelSort] classes; no order instances are introduced. GenerationFailureSource binds its failure result to the original later-defined FailedTurnEvidence through a native forward type argument, not a generic wire record. Actual source/decoder fields remain concrete.

## The assignment ownership repair

The owner explicitly approved the zero-or-one Todo API (523ba958cb42). Original code at planning/goals.py:555-556 selected `todo.pending` and then called `todo.order.instructions(todo)` in todo order. Original `Todo.pending` at planning/orders.py:5110-5111 is exactly `bool(self.order.items_with(Pending, self))`.

The plan adds **Todo.goal_instructions() -> tuple[str, ...]**:

```
return tuple(item.order.instructions(item)
             for item in self.order.items_with(Pending, self))
```

The goal joins `text for todo in todos for text in todo.goal_instructions()` with the original separator. Selection remains one original Pending capability query per Todo; each selected item is that same Todo. A pending order emits exactly its original string, including an empty string if that is what it returns. Nonpending/folded orders emit no strings, so the plan adds no separators for them. Todo order, original string values and progress_line are preserved. The original Todo.order: ProblemOrder field and Todo.pending property remain unchanged for other consumers.

GoalAssignment's live contract therefore exposes the owner-produced instruction tuple and progress_line, rather than pretending that every concrete order has an instructions method. No structural substitute, fact surrogate, copied store, broad exception, default method or persistent state is added.

This is an authored semantic source change with the determining source equation above, not an automatic behavioral proof supplied by NRA's exact patch. The implementation owner retains actual consumer verification.

## Concrete import-order obligations — NOT silently closed

The following original guarded/local imports remain in provider modules touched by this plan. They are not converted into new local imports, and this plan does not claim whole-module runtime-hint closure for them:

1. **activity_clocks.py:13-15** guards actual Channel and Message callback annotations on ChannelActivity. Separately, ChannelActivity.writes at :21-25 intentionally uses ClassFact to defer bus_activity_index / bus_display_index references. bus_activity_index.py:46 eagerly subclasses ChannelActivity, so replacing that metadata's lazy reference with an early unconditional table import is not equivalent. SentClocks membership on ActivityClocks does not settle these other callback bindings or SQL metadata timing.
2. **messaging/channels.py:32-35** guards RosterVisibility, Thread and RegistryProvenance. Their consumers include ChannelMemberIndex's actual thread inputs, visibility selection and channel history/display-scope methods. Adding the two display/order read capabilities preserves the concrete Channel fields but does not make these names appear in that module's globals. Their normal global binding/cold-entry closure remains an explicit obligation.
3. **channel_management.py:25-27** guards CatalogDocument, TagChoice and ThreadManagement. Registry ThreadManagement imports **RelationshipStore** at registry/thread_management.py:63. Therefore importing ChannelManagement into relationships merely to obtain its catalog read would make the guarded reverse-service dependency worse. This plan instead moves the original catalog read contract below both. A normal runtime binding or further owner cut for the three original guarded callback types is still required; do not mark the provider module fully closed after a base-list change.

Other selected provider/contract modules currently contain no guarded/local import bodies reported by the retained targeted source search. Unused TYPE_CHECKING imports are not themselves an unresolved callback; they should be removed if encountered during final cleanup. This is a bounded source observation, not a repository-wide assertion.

The implementation owner must check standalone cold roots at least for display_sources, activity_clocks, published_bus, messaging.channels, presentation, channel_management, relationships, history_views, goal_work, planning.goals, planning.orders, registry.threads, goal_attempt_phase, goal_failure_observation and goal_ledger after composing the current plans. That list is an execution obligation, not a test run performed here. The independent 17-runtime-role and message/task plans may affect the same transitive initialization paths; integrate against their final combined source rather than blessing this plan in isolation.

## Generator correction after the owner burst

Removed the private PROVIDERS class roster (MEMB-1). The bounded census comes from NRA's existing ModuleSyntaxIndex; conditional/nested rows remain visible rather than being invented as direct declarations.

The next owner burst exposed the remaining second verdict owner (IDEN-5). The generator no longer writes summary.json or runs its private AST preservation checker. simulation.json is now the public NRA execution_payload without a second verdict wrapper; projected.diff remains a derived source view. Runtime preservation still requires the implementation owner's real consumer checks and is not supplied by the syntax report.

Static parsing passed after these corrections. No scan or simulation was rerun. Existing plan.json, projected.diff, summary.json and preservation receipts remain historical records of the earlier delivered run; none was rewritten to manufacture a new passing result.

## Guarded-import runtime correction

The later guarded-closure-4.plan.json is **invalid on its own**: the integrating cold check (920e3f921f5a) found channel_targets → activity_clocks → messages → channel_targets. Its clean source simulation was not runtime qualification.

The clock correction is retained in guarded-closure-6.correction.plan.json: state-free SenderClockSource/MessageClockSource sit below destination initialization; MessageInput retains Timestamped through its new parent; ThreadRole inherits the original abstract clock API. The integrating peer confirmed that first cycle gone, then found the eager concrete Thread/RegistryProvenance dependency in channels (8d50726e65b9). V6 alone is therefore not a completed runtime cut.

The current complete proposal is guarded-closure-8.plan.json. Append only guarded-closure-8.channel-correction.plan.json after an already composed v6 projection. It factors the original TaskOwner role read into ThreadRoleSource, composes that with existing TaggedThreadSource as ChannelMemberSource, and adds that parent to actual Thread. ChannelMemberIndex and member callbacks retain their concrete member through a live type parameter, not a generic wire record. Existing RegistryNames[TaggedThreadSource] supplies the captured namespace reads. GroupedChannel inherits its original history-target contract through ChannelHistorySource; MemberActivityDisplayScope.capture keeps its original body and imports that low contract instead of TagChannel. Wire fields, defaults, provider calculations, member/alias selection order and deferred SQL writers are unchanged.

Native execution: guarded-closure-8.simulation.json / guarded-closure-8.exit. This is clean source/preflight replay, not a cold/MRO/consumer result; those remain in the integrating peer's existing combined run. No product source was applied by this generator. Candidate 5's payload-field error and candidate 7's edit-span overlap remain in their original logs/exits.

## Required post-integration checks (not run here)

* Actual provider concreteness and MRO; no instance dictionary introduced to original slotted values by role bases.
* Original dataclass/FieldCodec fields, SQL metadata and frozen defaults remain exact, including concrete Channel/GoalDeclaration/ProblemOrder/ProcessState/reservation/evidence types.
* Generic live role arguments resolve to the intended original owners/results; this does not claim specialized generic wire-family support.
* Real pending, nonpending, folded and empty-instruction cases preserve output ordering and separator behavior.
* Original acquisition/transaction/clock ordering, publisher authorization and deferred ClassFact projection discovery stay unchanged.
* Native consumer proof belongs to comms-friction. No direct source application or final P23 completion is claimed by this artifact.
