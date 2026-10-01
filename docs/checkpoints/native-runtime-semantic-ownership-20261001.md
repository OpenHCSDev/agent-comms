# #489: one native runtime lifecycle semantic pass

Integration owner: Arendt. Source inventory: `cfed0264070d5e1958e451fc44eb98f89e58c3d0`
(normal merge of #491 into the existing #489 work). #493 production remains frozen
at `33f9118c`; its additional source receipt is `5265fb70`.

## Receiving scope and execution order

The latest user instruction stops symptom continuation and transfers the whole
native lifecycle semantic pass here. Search and read existing owners, their facts,
decisions and consumers; implement one coherent change extending those owners;
delete replaced declarations and decisions in that change; validate LAST. No tests,
reproducers, native calls or provider calls run during investigation/design. Earlier
results below the old checkpoint's historical heading remain evidence of those
completed runs, not the ownership design. No APPEND/AGENTS edits.

No parallel PayloadBudget, RetainedContext, SessionIdentity, Cursor or Custody
family will be introduced. A class declaration occurring once does not establish
unique authority: every competing decision must also disappear from consumers.

## Implemented phase/replay relation

[Phase/replay source receipt](native-phase-replay-source-ownership-20261001.md)
records the supplied patch's receiving implementation in this same draft. Phase
members cross the watchdog event boundary; shutdown is an existing-family member.
Replay joins/revocation belong to ReplayAssessments. Watchdog replay booleans and
all proxy writers are deleted. Three retry observations share their original
PiEvent fields/behavior and the event is carried directly, without session copies.
Complete authored Core and pinned Toad searches distinguish the watchdog observation
from the persisted lease projection. No persisted watchdog-event reader was found
in that scope. Production: 172 lines deleted, 120 added across ten files. This is
implementation progress, not final validation or completion of the whole lifecycle.

[Acquired resource closure](native-evidence-resource-lifecycle-20261001.md) at
`dac8c161` places borrowing/acquisition/refusal cleanup on NativeEvidenceRead/Scope,
removes recursive consumer acquisition, and shares one acquired reader through
continued-session and recovery corroboration. Semantic coverage/proof/cursor
authorities remain their original owners.

[Session/revision source closure](native-session-revision-source-closure-20261001.md)
normally receives main union `1f8bf0ef` and Sch's framing `cc534692`. The existing
NativeWitness extends NativeSessionIdentity; FileRevision crosses native witness,
attestation, result and publication fields via the existing NativeRevisionText
capability. All native_stamp/repeated revision-decoding consumers are deleted.
Paused writer/budget and canonical terminal/readiness work remains in this draft.

## Actual existing-owner search

[source-owner-search.json](../../evidence/runtime-lifecycle-semantic-pass-20261001/source-owner-search.json)
contains the original hashes of 347 authored Python/native files, declarations,
typed fields, methods and their conditional expressions for 83 existing owner
names; all lexical owner references and same-named method-call candidates are
included. It excludes tests, installed/compiled dependencies and private history.
These are source search results, not execution evidence or resolved dynamic calls.
The entire authored source scope is searched; no omitted Python parse errors were
reported. Every named declaration can be inspected directly in the artifact.

81 names have exactly one declaration. Two require qualified meanings:
`coordination_tables.attempts.AttemptRecord` records an execution attempt;
`input_disposition.AttemptRecord` stores an original ingress disposition.
`turn_lease.TurnState` projects the canonical lease; `agent_events.TurnState`
reports watchdog observations. Shared spellings alone do not make these the same
fact. The source map records both locations rather than claiming uniqueness.

The raw same-named method candidates include unrelated receivers. Resolve each
receiver and inherited declaration during caller closure; do not count that raw
list as completed semantic consumer review. Python nested declarations and typed
fields are included. Native JS decision locations are separately traced below.

## Semantic fact map and complete caller trajectory

The table names the existing determining owner, its distinct storage/proof/view
relations and the consumer sets to close. The artifact provides every exact named
reference and Python declaration/method location behind these sets. A projected
field is not classified as a competing decision merely because it stores a value.

| Fact | Existing determining owner | Other roles and consumers to close |
|---|---|---|
| Selected model capabilities and current native configuration | Native `StateData` / `PiModel` observed through `NativeSessionPreparation` | `SelectedModel` is a selected-compaction projection. Registry model/thinking is configured intent, not reconstructed native capability. `OwnedTurn.prepare_native`, `TurnRunner.prepare_selected_session`, adaptive/manual/bridge and route/settings readers consume the original state. |
| Final transformed request input estimate and generation admission | `ContextBudget` | `ContextBudgetRequest` owns prestream request/rejection behavior. Request-input projections and provider adapters supply their FINAL serialized body after hooks. `SessionContext`, selected decision and next-dispatch compaction must consume that relation rather than decide context admission independently. |
| Explicit generation intent versus model output capability | Original provider options consumed by `ContextBudget.allowance/admit` | An absent API allowance stays absent. Provider count rejection may narrow only an unaccepted request; accepted stream and original input are never replayed. `ContextBudgetRequest`, all patched provider builders and selected summary bindings are consumers. |
| Summary scheduling, input packing and retained visible output allowance | `CompactionPolicy` / `CompactionPlan` | These own resource allocation and visible output accounting, not a second model-capacity truth. `inputBytes/contextFits/packSummary/summaryTokens`, map/reduction and selected provider stream must derive admission from `ContextBudget`; preserve reasoning/output contract. Sch #494 owns retained frame consumers. |
| Source task facts | Original goals, input dispositions, wire and owner records; `RetainedTaskFacts` renders their captured projection | `HeldCompaction.capture`, `CompactionSource`, `SelectedSummarySource`, journal, native request and commit/recovery consumers. Sch #494 owns metadata/content framing, including the entire-envelope 64KiB error. Do not copy task facts into a new authority or truncate original mandatory facts. |
| Native logical session identity | `NativeSessionIdentity` | Header, live attestation, reopened SessionManager and summary witness corroborate one original session. `NativeAttestation`, `NativeCustody`, `StateData.identity`, `NativeWitness`, route/fork, context and selected summary consumers use that identity. A path/stat is not enrollment. |
| Physical file identity, revision and private role | `FileIdentity`, `FileRevision`, `FilesystemRole` | Original POSIX observations, not permission/proof. `SessionRevision` adds the separate original `.input-proof` observation; fresh startup, evidence reader, reopen, selected reservations, commit/cut/recovery must not recode tuples or independently classify modes. |
| Fresh-session enrollment | `FreshPrivateSession` | Minted RAM-only custody and original enrollment record; never reconstruct from file identity, path, SQL absence or decoded history. Startup, selected route/preparation, compaction journal and native writer consumers preserve the original mint. |
| Cold native startup admission | `NativeStartupAdmission` | Acquired startup permit and selected attestation, not a turn success state. `PersistentPiSession.open`, ordinary/selected preparation and `TrackedTurnSession.attest` use the same owner. Startup release already occurs at attestation; do not claim it is held through model generation. |
| Child/connection/writer custody | `NativeCustody` family, owned by `PersistentPiSession` | `PiSessionChild` owns actual process/channel/stderr; `TurnSession` owns receive correlation while borrowed. Empty/reopen/borrowed/retained/retiring cases already exist. Compaction needs paused predispatch behavior on this existing family, not another session store or reacquired reader/writer. |
| Original irreversible prompt admission | `PromptAdmission` and the original `send_fenced_prompt` writer | Unwritten/acknowledged/started/settled are distinct producer facts. Raw partial write is UNKNOWN; no-byte pre-admission witness is not a historical UNKNOWN override. `TurnSession`, `TrackedTurnSession`, `PrivateSendAdmission`, native startup and input forwarders consume the owning writer receipt. |
| Original user/goal/follow-up input disposition | `InputAttempt` family owned by `InputDispositions` / `InputDocument` | Storage preserves Reserved, BoundUnknown, Started and NotSent. `OriginalTurnInput`, `OwnedTurn`, drain, goal scheduling, compaction eligibility and ACP failures consume original rows, not inferred prompt/terminal flags. Preserve old UNKNOWN and exact pending/future-queue membership. |
| Original coordinated native input identity | `NativeInputIdentity` and `NativeInputExecution` | `NativeRuntimeInput` is the original reservation/context record. `NativeSendStage`, `PromptBinding`, native rules, tracked events, historical/recovery readers and cursor owner must use its original input/stage/execution/owner relation. |
| Input's frozen source membership | Existing `NativeInputExecution` behavior and original execution assignment links | Kepler #490 owns FULL membership via `ExecutionAssignmentLink` and TRIAGE preexecution `TriageNativeSources`. One input may cover several assignments. Historical per-source membership is a join projection, not part of input/session identity. Every historical, binding, coverage and cursor consumer must follow that original relation. |
| Live assembled context proof | `NativeContextProof` / original emitted commit observations | `NativeContextJournal` is original immutable corroborating storage; `NativeInputReference` is the complete recorded relation. `TrackedTurnSession.context_proof`, send verification, continued-session admission, source history/cursor and recovery cannot mint proof from decoded bytes. |
| Exact prompt-to-original-source binding | `PromptBinding` | Original prelaunch immutable binding, exact digest and original frozen membership. `bind_expected_prompt`, native binding rules, send/commit, historical and recovery consumers must not independently rebuild a prompt or grant admission from a digest alone. |
| Acquired source byte lifetime | `PrivateEvidenceRead` → `NativeEvidenceRead` → `NativeEvidenceScope` | Resources and decoded bytes only. Each observation rechecks source/prefix/ancestors. Optional borrow/acquire recursion is repeated acquisition behavior, not nominal state polymorphism. Close it at the existing resource owner across context/prompt/historical/coverage/continued/recovery consumers. |
| Frozen addressed source coverage | `SourceCoverage` using original checkpoint/audience/cohort and native proofs | `ProvenSourceCoverage` is the derived whole-prefix result. Wake-mode membership, exact native prompt and UNKNOWN gaps determine coverage. Historical maximum alone cannot produce it. All cursor read/advance/source readers use this result. |
| Current-owner cursor eligibility and position | `CursorOwner` + `NativeSourceCursor` | `CurrentNativeCursor` is derived SQL projection; read/advance reprove prefix, participant/admission and original input. `CursorPublication` only owns delivery ordering/dedup. Selected result and ACP metadata consume it; no work/replay authority follows from cursor position. |
| Registry owner, admission and participant identities | `OwnerIdentity`, `AdmissionIdentity`, `RegistryOwner`, `ParticipantOwner` / `OwnerGenerations` | These are DIFFERENT allocation domains. Registry captures, native stages, selected session, cursor and recovery must preserve domain meaning and full process proof. Mutable owner routing name is distinct from original ACP logical session. |
| Current turn lease and original input audience | `ActiveTurn` / `TurnLeaseFence` held by `OwnedTurn` | Registry is canonical; `TurnProgress`, queue/phase/goal/status projections derive it. Permit/lease/inbox resources retain acquired settlement ordering. No new lifecycle store or copied busy/current owner flags. |
| Execution attempt and recovery settlement | `AttemptRecord`, attempt lifecycle and `RecoveryMonitorCapability` | `MonitorEvidence` carries original witness observations, not a retry grant. Current execution pointer, owner fence, backend/process proof and original frozen publication relation determine settlement. Recovery cannot translate absent native history into NotSent or replay UNKNOWN. |
| Retry/replay safety | `ReplayAssessments` with `ReplayFact` | Original monotonic SQL relation and execution/obligation authorization. `AttemptStore`, recovery reader/monitor and retry consumers use it. Watchdog observations must not classify a second replay policy; supplied `TurnExposure` is not accepted as an additional authority. |
| Native phase and observed request progress | `TurnPhase` / native `RequestProgress` producer | `ProgressWatchdog` observes for transport liveness; registry lease publishes canonical phase. `NativePhaseChanged`, `TurnProgress`, transcript/ACP and Toad consume typed phase. Diagnostics keep original native clock and local callback spans separate. |
| Native original input/context commit observation | `NativeCommitObservation` in `TrackedTurnSession` | Pending versus observed carries original emitted receipt. Native SQL corroboration, attestation, tracked tool/context and final result consumers cannot substitute a boolean or stored context copy. |
| Native terminal result and output publication | `TrackedTerminal`, `NativeTurnResult`, `TurnFailure` and `TurnOutput` | Tracked versus ordinary transport representations have different responsibilities; compare complete producer/consumer semantics before unifying. `OwnedTurn`, durable selected execution, goal completion, `TurnProgress.publish_result`, diagnostics and ACP must derive one terminal witness, not bool+optional terminal reconstruction. |
| Usage and terminal statistics | `UsageAccount`, original `PiUsage`, `StatsRequest` | Response usage is original provider data; stats collection is a resource/request lifecycle. Context estimate and retained visible output must not double-add reasoning or reinterpret a missing stats reply as acceptance. |
| Compaction source custody and commit permission | `SelectedSource`, `CompactionBoundary` / `HeldCompaction`, original journal roles | `CompactionSource` is captured projection. Reserved summary result is NOT commit permission. Manual/adaptive/bridge, selected slot, native writer, commit intent/outcome/recovery/publication must consume original source/input/writer custody. Idle reacquisition and active paused continuation must be different capabilities of the SAME existing owner. |

## Deletion trajectory, not a second architecture

1. Move all request preparation/admission decisions onto existing `ContextBudget`;
   delete independent `shouldCompact`, context-window subtraction and final-message
   admission checks in `patch-native-auto-compaction.py`. Derive packing allocations
   through the existing `CompactionPolicy`, with Sch's original content frame.
2. Extend existing `NativeCustody`/`TurnSession` ownership for the paused request;
   delete idle reacquisition and child retirement assumptions in those callers
   once the same original writer owns summary commit and continuation. Do not call
   `admit_prompt` a second time or create another pending request/input store.
3. Place borrowed/new-reader acquisition at the existing resource seam. Delete
   acquisition recursion from consumers and independent rereads in
   `continued_private_session` / `attempt_recovery`, preserving every original
   context/binding/coverage proof and source mutation refusal.
4. Pass typed phase across the observation boundary; remove replay decisions from
   watchdog event construction. Existing replay owner receives original observations
   and owns monotonic policy; consumers must not classify them independently.
5. Close source/input/membership/cursor decisions with Kepler's original producers,
   remove replaced single-assignment assumptions only after the original storage
   preservation seam is defined. Close failure settlement and derived Ready/loading
   publication from the same canonical terminal/lease result, not a UI patch.

This is a source inventory/design checkpoint, not a claim that those deletions or
all dynamic caller resolution are complete. No new type is justified just because
the table has a row. Any new member must add missing behavior to an existing owner;
a genuinely new root requires an existing-owner search showing none owns it.

## Supplied patch 36471530: receiving decision

`0001-turn-state-crosses-its-boundary-as-itself.patch` was read in full.
Typed `TurnPhase`, member-returning `stalled` and a shutdown member belong to the
existing phase family. Do not blindly apply `turn_exposure.py`: its `TurnEffects`
duplicates an existing ACP-effects name and its booleans/ranks reconstruct exposure
to answer replay safety independently of `ReplayAssessments`.

Actual source search: `events.TurnState` producers are watchdog `state` and
`TurnSession.identity_uncertain`; no specialized `AcpEventConsumer` handler consumes
this event. `TurnProgress.consume` forwards typed events; ACP dispatch publishes
SDK/extension updates by registered handlers. `runtime.RuntimeServer` transports
those updates rather than serializing arbitrary AgentEvent dataclasses. Canonical
phase metadata travels separately through `TurnTranscriptUpdate` / `TurnChangedUpdate`
using `turn_lease.TurnState`. Diagnostics serialize `Done`, reason and measurements,
not `agent_events.TurnState`. Therefore the supplied blanket claim that this event
has an encoded persisted wire shape is not adopted without a concrete consumer.
Search Toad's current public extension reader as part of final caller closure;
do not confuse the two records by their common name.

## Original storage and joint crossings

Kepler's dirty #490 removes `assignment_id` from `NativeInputIdentity`,
`NativeInputReference`, `NativeRuntimeInput` and `CurrentNativeCursor`, and changes
native schema 4→5. Original coordinated input reservations/context and UNKNOWN
references cannot be reset as if they were cursor projections. Preserve original
IDs, context group, digest, admission and source membership at a reviewed one-shot
outside-src storage seam; no product legacy reader or reconstructed reservation.
The cursor remains a derived projection only after those originals survive.
Kepler owns the target producer/membership declarations; #495 owns the installer-only
preserved schema 4→5 carry through the existing stopped-owner installation family.
#489 owns consuming custody and adds no carry/reset implementation.

Existing `CompactionPolicy.packSummary` already checks mandatory retained text before
generation and final packed context afterward. Reuse that behavior; add no second
native preflight or Python estimator. Its resource allocation must consume the
original request-local native budget relation. Any required capacity fields belong
to existing `NativePreparation`, not a new payload budget or capacity store.

Schrodinger `01a0ef00-6563-7ec0-9c64-564ece67a6eb` owns #494 retained metadata/content
framing. Shared `compaction_boundary`, `selected_pi_summary_rpc`,
`owner_compaction_runtime`, `selected_source` and `compaction_records` require one
integrated contract before crossing edits. Sch's typed source/frame must expose
original content and metadata to the existing lifecycle and budget owners, not
introduce an overflow store or caller-specific size exceptions.

Original native sessions, `.input-proof`, wire, goals, decisions, failed diagnostic
inputs and UNKNOWN are protected. Compaction operation journals are declared runtime
state, separately classified from original inputs. Any changed runtime format is
cut over only under the existing authorized stopped-owner lifecycle, parent sole
public executor. No reset/retry/replay during this source pass.

## Owned files and closure evidence

Arendt owns authored native budget/session/compaction preparation lifecycle in
`stack/native-context-budget.mjs`, `native-session-context.mjs`,
`native-compaction-policy.mjs`, provider/input and automatic-compaction patchers;
Core native acquisition, attestation, custody, tracked/backend, original input/proof,
coverage/cursor, continued-session/recovery and canonical terminal/phase consumers
listed in the table. All references in other files are receiving consumers, not
permission to edit another agent's dirty source. Kepler owns selected batching and
membership producers; Sch owns retained frame producers; Heis owns physical UI.

Publish declaration/reference/decision searches again against the COMPLETE combined
diff before final validation, showing determining fact owner exactly once and every
replaced decision absent. Unique class counts alone do not discharge that obligation.
Patterns: IDEN-1/3/5/8, IMPL-10/13/14, BOUND-1/2, TIME-9. Source semantics choose the
change; batched sanity and the installed original native/ACP/UI journey run LAST.
