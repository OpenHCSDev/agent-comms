# Item 5 — newly added None sites in #457 / #462

Reviewed versions: #457 `e1f2d732bb9c8f447da12b5fbc0b31519860cdc0`,
#462 source `c53449cb6fdefac52152f79f23d66ba6ba6c9340`.
No production bytes or frozen ea8/c075 cohort were changed for this review.

`none-site-census.json` records every added production line containing `None`
in the four own #457 source commits, their accepted peer integrations, and the
#462 source commit: 110 text sites. This intentionally includes moved existing
checks, repeated signature propagation and unit-return annotations; it is not
a count of 110 new semantic states. The retained #457 baseline source is
6feb634ba184d94d763028406152fff07027cccc.

## Concrete remaining builder defect — Arendt owns correction

The newly extracted `NativeInputContext.reference` in the integrated native
builder checks only `input_id` and `session_id`, then constructs the required
`NativeInputReference` with optional `assignment_id`, `stage`, and
`request_generation`. It does not itself establish a complete acquired proof.

Measured on a **disposable SQLite backup** of the completed real private native
fixture, leaving its original DB, journals and inputs unchanged:

- Original current cursor: input `17e3825cee3cf30eccb0a057b8e53fe7`, request
  generation 1.
- Updating only `current_native_cursor.request_generation` to SQL NULL was
  accepted by its current check and trigger.
- `CurrentNativeCursor.one(...).reference` returned a nonempty
  `NativeInputReference(request_generation=None)`.
- SQLite's check admits the partial group because `request_generation>0`
  evaluates NULL; `CHECK` does not reject that result.
- Updating `NativeRuntimeInput` was correctly rejected by the existing frozen
  identity trigger. This witness does **not** establish that such an update
  bypasses that native-input fence.

See `none-context-shape-probe.json`. The public getter's existing
`CurrentNativeCursorOwner._require_source` proof-reference comparison rejects
this cursor against its complete original journal proof. No replay, native
authority bypass or live corruption was demonstrated. The confirmed defect
is construction/admission of an incomplete supposedly acquired reference.

Arendt explicitly received and owns canonical complete native-proof/reference
construction and all its consumers after his bounded #463 installed gate.
He requested the witness, which was sent directly. No competing builder patch,
new optional-state wrapper, schema operator or public repair was made here.
This item is **open**, not classified away as external SQL absence.

## Own #457 / #462 sites: declaration and caller rationale

| Added sites / declaration owner | Meaning and complete caller relation | Review disposition |
| --- | --- | --- |
| `AssignmentState.wake_frame` obligation checks | Existing original `ResponseObligation` lookup may be absent. `TriagePendingAssignment` forbids an obligation; `EngagedAssignment` requires one and validates original execution/target and declared preparation lifecycle. `wake_injection` calls the declared family operation. | Internal resource absence, not an inferred lifecycle or a new stored status. |
| `AssignmentState.build_preengagement(mode, None, None)` / `preengagement` | The existing pending declaration constructs its mode without execution/target. Only declared preengagement targets/successors may use this builder; an already bound execution is refused. `AssignmentStore` invokes it and persists the existing row format. | Existing declaration-owned unbound resource shape; no new field or None-based family selector. |
| `ExecutionState.has_retry_budget`; `DeferredExecution.can_retry`; `CoordinationSnapshot.can_retry` | Ordinal/attempt absence already exists for executions without an acquired attempt. State-specific snapshot validation remains canonical. Deferred retry requires an original ordinal, actual terminal attempt finality, explicit replay authority, and a noncurrent pointer. Both snapshot and read-only recovery projection invoke the same lifecycle operation. | Internal absence of acquired attempt; denies retry. It does not translate absence into failed/done status. |
| `ExecutionRecord.retry_authorized(replay, obligation)` | The existing replay record and wire-response obligation are optional related SQL rows. No replay record means no authorization; a wire execution needs its actual retryable obligation. `CoordinationSnapshot.retry_authorized`, validation, recovery and attempt disposition consume this same owner decision. | Storage lookup absence plus conservative authority refusal; no synthetic safe-replay record or default permission. |
| `ClaimOwner.admission` shape check | Existing manual claims can lack a native wake admission. A present admission must be the actual typed `WakeAdmission`; claim-only transition admission rules and selected-source comparison require the original admission for native wake. | Existing optional capability, not a pending/active flag. No new field introduced. |
| `BoundUnknownInput.started` returning None | Exact original turn/native/text matching owns transition to `StartedInput`. Mismatch yields no transition. `InputDispositions._transition` leaves the original row unchanged; other declared input members still own started/unknown/cancel behavior. | Optional transition result; no None stored as an input lifecycle and no disappearance/replay of UNKNOWN. |
| `Assignment.owned_by`, `ChangeTodoState.generation`, `Todo.require_assignee/require_actor` | A creator's state operation may omit an assignment generation. A different actor must present the exact original assigned owner and generation; None cannot match a real nonempty generation. `TodoStore.set_state` constructs the declared command and `TodoState.decide` / `DoneTodoState.decide` own authorization. | Optional caller capability, not inferred assignment status. |
| Todo `assignment=None`, `require_assignment(None)`, done/release operations | The original durable Todo row already has optional assignment. `OpenTodoState` alone admits assignment into an unowned resource; transfer/release commands compare exact previous assignment, revision and operation provenance; `DoneTodoState` owns completion authorization and clears the resource. Store callers invoke these commands. | Existing resource ownership absence kept in the sole durable row; explicit lifecycle remains `TodoState`. No duplicate unassigned flag/store. |
| Todo `last_transition` optional declared type / default | Before #457 the same field was `Literal[transfer, release] | None`, with the same default and `last_previous` field. #457 changes the nonempty values to `AssignmentChange` declarations. Exact uncertain-reply retry compares the complete current result including declared transition and previous assignment; new operations still require exact revision. | No added nullable storage state. Absent provenance never authorizes transfer/release retry. |
| `QueuedInput.after_clear` and `InitialInput.after_clear`, with input-drain filter | Result selects zero or one existing live input resource to retain after clearing followups. The original initial dispatch remains owned by `InitialInput`; normal queued resources are removed. The source disposition is not rewritten to None. | Internal resource-selection result; declaration owns behavior. Not SQL/JSON, not a cancellation state. |
| `QueuedInput.restore_after_turn`, `InitialInput.restore_after_turn`, finish filter | Result selects a bounded editor/display restoration resource. Initial input is not restored after terminal completion. Native dispatch eligibility comes from the original typed dispositions, admission and source; restored display rows are not automatically rerun. Queue projection derives current/restored resources separately. | Bounded presentation resource absence; no semantic terminal-state mirror or replay authority. |
| New optional `input_id` propagation: `TurnRunner.prompt`, `InputDrain.run_owned_input`, `InitialInput.capture` | An ACP request can supply its original identity; callers without one generate one via existing `ACPInputIdText.new`. Present identities pass the same decoder. Initial native event may omit an ACP ID; `input_started` resolves it from the retained original resource/disposition and emits its actual public ID. | External/request identity presence at ingress; it does not determine started/sent/failed status. No new ID registry. |
| `input_started` optional display text and `dict.pop(..., None)` | Display text comes from the retained queued resource or exact original notice identity after `require_started`. Missing text omits an optional rendering payload; the proven public/native input identity and its disposition still publish. `pop` removes a resource if present. | Optional presentation data/lookup result; input lifecycle is the typed original row, not text presence. |
| #462 `Goals.consume_reply_wait` missing current/captured goal or wait | Existing `Thread.active_goal` and `GoalWaits.for_goal` may have no relevant resource. Consumption requires matching original active goal, wait owner incarnation and certified original reference, then existing wait-ID CAS. Successful full publication/committed ignore invoke it; failed/UNKNOWN/preparation do not. | Absence prevents mutation; it does not manufacture a goal execution state or a successful reply. |
| #462 `allows_wait(GoalWait | None, Comms)` signatures and captured wait None check | These nullable arguments/checks predate #462; the change replaces mutable-registry sender inference with original certified source resolution. `OrdinaryDependencyRule` checks captured wait identity first; `OrdinaryWaitRule` uses the same source declaration. Existing direct owner inputs remain wait-independent. | Existing absence of a dependency declaration, not new state. #462 deletes the competing copied queued wait instead of adding an alias or retirement flag. |

`-> None` on command/validator methods is a unit return annotation. It does not
carry an absent state. Regex `fullmatch(...) is None` is the library's match
result contract, and the explicit external scalar decoder rejects malformed
identities/digests; no semantic selection is based on regex failure.

## Peer builder integrations included in the census

| Sites | Owner / rationale and limit |
| --- | --- |
| Arendt `NativeInputIdentity.execution_id/attempt_ordinal` | Existing triage versus full original native reservation dimensions, supplied by `TriageNativeSend` / `FullNativeSend` and compared as one original identity in selected tool admission. This extraction added no database fields. Neither missing value grants full tool authority. |
| Arendt `NativeInputContext` optional context group / `reference` | **Confirmed incomplete-reference defect above.** Owner: Arendt; not closed by typed annotation or SQL taxonomy. |
| Arendt cursor/attempt recovery missing original row | `CursorOwner._matches_input`, `require_recorded_input`, and `NativeLossProof` reject missing/mismatched original input. Cursor source expectation uses None only for injected sequence zero: `SourceCoverage.last_proof` raises for a nonzero sequence without corroborated proof. Therefore absence cannot prove a nonzero injected input. |
| Arendt selected tool `session_id/verdict is not None` | Existing acquired/settled native resource fields, not newly introduced schema. Original identity + sent admission must match; settled records cannot reuse a tool capability. Complete acquired-proof construction remains the named builder gap. |
| Sch projection `checkpoint/snapshot`, optional file revision/cache/projection | Original opened file revision controls bounded disposable append/display checkpoints. Missing or invalid checkpoint/revision causes a rebuild/scan of original records; `display_view_metrics` does not treat absence as empty authoritative message history. Atomic writers' unit returns carry no state. Owner: Sch bus/index projection; these are disposable resources, not delivery/handling authority. |

No newly introduced own nullable field establishes pending/sent/failed/UNKNOWN
by absence. That conclusion is limited to these exact diffs and consumers;
it does not close all historic nullable native acquired-proof shapes. The one
confirmed builder construction gap remains explicitly assigned to Arendt.
The installed #462 acceptance receipt remains valid for its tested coherent
source; no repeat native/provider/UI gate was performed for this read-only
review. Parent owns the merge decision with this named dependency visible.
