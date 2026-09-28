# Original C0 / S1–S8 / A1–A10 completion audit

2026-09-28 · Darwin · read-only source/evidence audit; no implementation or runtime action.

**Current follow-up:** O1 source closure and updated main202 live evidence are in
`../r7-vocabulary-closure/COMPLETION-AUDIT.md`. This original receipt remains
historical; its O1/activation-pending findings are superseded there.

## Verdict

The combined candidate has the original major ownership replacements, including
all seven R1–R7 replacement surfaces. It is **not yet an unqualified original-plan
or whole-goal completion**: R7 missed a small, concrete portion of its internal
assignment/lease vocabulary migration; literal S4/S7 structural guards remain
unmet; some original replay/performance acceptance was never established by the
receipts inspected. Final installed queue/live activation is parent-owned and
pending in the current integration handoff. None of these observations imposes
a CI gate or authorizes duplicate provider tests.

The exact obsolete API families checked below are deleted. The residual naming
finding is on the *current* determining owners, not evidence that the deleted
executor, aggregate, state family or ACP facade still exists.

## Source and authority

- Original requirements: dispatch `plans/C0-carve.md`, all `S1-*` through `S8-*`,
  `plans/01-shared-abstractions.md`, and index acceptance. Reconciled with
  `OWNER-DECISIONS.md`, `DELETION-AUDIT.md` and `FINAL-ORIGINAL-PLAN-AUDIT.md`.
- **C** = parent `/home/ts/wt/comms-c0-integration-20260928`, candidate
  `ead4800988f0066993c120605dbbd20236dbb12e`. Its merge `fdf19eb` contains full
  R6/200 and R7/201. The subsequent source diff is only `threads.py` and
  `pi_payloads.py` reusing `FieldCodec._types`; the rest adds parent evidence.
  These two narrow cache edits remain parent-owned.
- Source references below are relative to `C/src/agent_comms/`; test/evidence
  paths are relative to C. This audit writes only in Darwin's own R7 tree.
- Paired current Toad tree was inspected read-only:
  `/home/ts/wt/toad-export-caller-migration-20260928`, HEAD `875e4ea` plus parent
  working change to `tests/channel_history_reader_pilot.py:58`, now
  `lease_local_turn`. Parent's combined handoff confirms the installed pilot.
- `inspect_source.py` reads committed Git objects without importing application
  code; `source-inspection.json` records exact lexical/AST witnesses. This is
  not a detector score, runtime test, behavior equivalence proof or new census
  of feature debt. No suites/providers were rerun; no live roots were mutated.
- New owner instructions override temporary mixins/re-exports, compatibility
  factories, old-writer coexistence, feature freezes and full-CI merge gates.
  Current external formats, real saved data and UNKNOWN/no-replay remain.

## Shared abstractions: actual producer/consumer closure

| Requirement | Candidate source / current use | Assessment |
| --- | --- | --- |
| A1 derived family names, collisions, capability catalogs | `declared_family.py:16,91`; AutoRegisterMeta per-family ownership, affix derivation, collision rejection and `members_with`. Commands, states, goals, exports, input attempts and transcript declarations consume it. | Source fulfilled. No second native registry: Pi's external selector and the codec's family discriminator are separate boundary concepts. |
| A2 declared-field boundary codec | `field_codec.py:40`; `MessageWireCodec`, views, RuntimeRequest, goals, recovery, PiPayload, NativeEntry, TranscriptCodec and all migrated documents reuse it. Metadata owns external spelling; no hand-maintained Message field decode map remains. | Source fulfilled for named original scopes. Saved-data normalization is a real boundary, not an internal constructor alias. |
| A3 state-specific data, successors, behavior | `lifecycle.py`, execution/assignment/attempt/obligation/recovery families; `goal_generation.py:21`, `goal_attempt_phase.py`; compaction families. Stores decode into these and derive choices/transitions. | Source fulfilled. SQLite transaction owners remain real authorities, not replaced with JSON stores. |
| A4 declaration/MRO reactions | `mro_dispatch.py`, `agent_events.AgentEventConsumer`, `agent_event_updates.AcpEventConsumer`, DurableTurn; R6 synchronous transcript render consumers reuse the same handler declarations. | Source fulfilled; no new event/role dispatch registry. |
| A5 commands own parameters and behavior | `command.py`, `pi_commands.py`, `goal_actions.py`, `runtime_requests.py:39`, `cli_commands.py:92`; real socket/CLI parser and proxy call these declarations. | Source fulfilled. Runtime no longer projects unknown extension keys or decodes errors twice. |
| A6 correlation | `pending_requests.py` consumed by settings and Pi commands. | Source fulfilled; distinct protocol layers intentionally have separate instances. |
| A7 incarnation, owner, turn and admission meaning | `thread_identity.py:18–66`, RegistryDocument, Registration, TurnLeaseFence. DM proofs depend on incarnation; turn settlement uses the exact captured lease. | Semantic source fulfilled; residual *names* recorded under O1. Persisted epoch spelling is permitted at its single boundary. |
| A8 document ownership and shared reads | LockedStore.read/reading/update/replace; RegistryStore, ReadLedger, GoalWaits, GoalPauseEvents, RelationshipStore, PassiveAwarenessStore, ChannelCatalog, InputDispositions, AcpDeliveryCursors, RuntimeInfoStore, SharedLedger, ActivityCheckpointStore. | Named document adoption fulfilled. Append activity/wire and SQLite route/goal/coordination/journal stores are distinct storage roles; no fake cross-document transaction. |
| A9 exact painted basis | `read_basis.py`, `read_ledger.py:35`; sparse message membership plus viewer/peer incarnation and bus identity; HistoryViews/Toad pass the actual display basis. | Source fulfilled; scalar old markers cannot manufacture displayed reads. Delivery progress is separate from human reads. |
| A10 one internal event vocabulary | `agent_events.py`; backend, turn progress, participant, ACP and goal producers/consumers use typed events. | Source fulfilled. Native RPC and saved transcript formats are separate external boundaries, not competing internal AgentEvents. |

## Original surfaces

### C0 — composition and declaration regions

`comms.py:22` constructs explicit messaging, channel, agent, owner, goal,
transcript, thread, relationship and history components. It retains no moved
Comms methods. Domain declarations have direct imports. `operations.py` and
`declarations.py` are absent, with no production import of either aggregate.
The bus and registry were subsequently decomposed into WireLog/Publisher and
Registration/RegistryDocument/RegistryStore. These are actual state owners,
not shared-self mixins. C0 source is fulfilled; `evidence/c0-live/HANDOFF.md`
and paired Toad89 cover its earlier installation. That dated receipt does not
prove the new combined candidate is live.

### S1 — internal events, reactions, settlement

- Frozen event declarations replace backend dictionaries. ACP and participant
  consumers use MRO handlers; setting correlation uses PendingRequests.
- `TurnRunner.settle_turn` (`turn_runner.py:388`) separates stream settlement
  from terminal publication and releases waits in `finally`; OwnedTurn and
  manual bridge use it. StreamSettled, TurnSettled and NoActiveTurn are distinct.
- Goal authority emits GoalChanged; tool-name inference is gone.
- The later required failure-feedback seam is present in `OwnedTurn.run`
  (`owned_turn.py:850`): runner error reporting preserves cleanup/rethrow.
  Existing `test_turn_runner.py:142,168` checks one error and actual ACP updates
  for a compaction fault with no original send.
- `test_agent_events.py:84,129,160,179` covers extension through both consumers,
  terminal ordering and the internal string-dispatch guard. No dict event shim
  or TranscriptUpdate.from_legacy remains. Source fulfilled; T4 limit below.

### S2 — Pi RPC, phases, failure and usage

PiRpcChannel is the line boundary. PiEvent/PiPayload decode known message,
content, delta, usage and response data; PiCommand owns command/response meaning.
Unknown external payloads remain opaque. TurnSession owns streaming;
TurnPhase/excursion declarations own transition/stall behavior; TurnFailure
owns precedence/text/uncertainty; UsageAccount/turn stats own accounting.
`_stream_agent_events`, `_JsonLineReader`, known-event Mapping/wire and steering
aliases are absent. Native proof validators still check persisted authority;
they are not an additional ordinary RPC decoder.

`test_pi_rpc_nominal.py:48,93,105,139,163,183,188` covers extension, failure pairs,
golden formats, correlation, partial/cancelled lines and retired mechanisms.
`evidence/s2/HANDOFF.md` records ten deterministic old/new replay scenarios and
338 focused cases (overlapping later batches), explicitly **not** real captured
provider transcripts. R1's source/native receipts and installed R1 acceptance
close known nested payload adoption. Source ownership fulfilled; original T4
evidence remains narrower than requested. The current AST found no >100-line
function in backend/turn_inputs/turn_stats/turn_usage/pi_rpc; blanket package
size guards are a separate S7 observation, not an S2 regression claim.

### S3 — durable coordination and recovery

Assignment, execution, attempt, obligation and recovery families own state data,
successors and behavior. FieldCodec serves both recovery sides; no old state
enums, hand-maintained transition catalog, gateway vocabulary roster or
ProjectionRecord/RecoverySnapshot forwarding serializer survives. Real redacted
recovery schemas remain. DurableTurn consumes live phase events; selected
execution reads its current fence rather than keeping a second evolving copy.

`test_coordination_nominal.py:33,43,82,93,121,162,235` includes saved-name/edge
characterization, invalid state-data combinations, extensions through real store
and gateway, and phases recorded before completion. PR201 closes original
OPEN-6's procedure with single-use SelectedExecution. Source fulfilled apart
from the joint S5 naming omission. This audit does not retrospectively certify
an exhaustive one-to-one ledger of the original approximate 47 legality checks.

### S4 — routing, reads, serialization, presentation

BuiltinChannel owns built-in meaning/aliases; ResponsePolicy declarations own
recipients, start/separate-turn behavior, guidance and disposition keys; current
Message/ResponseEligibility/ScheduledTurn consumers use them. ReadLedger owns
human classification and exact displayed facts. Views and unread are projections.
Message, SavedView/ViewPredicate serialization is declaration-derived. Catalog
preferences, parents, pins, sorts, any-mode and saved views use one canonical
CatalogDocument; old files are one-way migration input, never a second writer.
BusPresentation is separate; inspected authority modules do not import it.

`test_read_ledger.py:19–178` includes bounded/any-mode/rebind/reopen and randomized
painted-subset properties; `test_response_policy.py:13` exercises a new policy
through five consumers. S4 mounted partial-paint receipt and R2 installed
8478-row/catalog comparison establish useful actual behavior. Source ownership
fulfilled; the literal no-built-in-string guard is not fully met (O2).

### S5 — duplicate authority and vocabulary

ResourceClaims store/test are deleted; resource envelopes remain the owner.
ThreadIncarnation, OwnerIdentity, TurnIdentity and independent admission counters
answer distinct questions. Current tests in `test_thread_identity.py` and
`test_read_ledger.py` cover turn-stable DM acknowledgement and rebind rejection.
Registry lease APIs and exact release fences replace old claim/release adapters.
R7 deletes WakeClaim/ClaimState, maps attention through WakeAssignment and uses
one participant_generation field. No production Python NAME token contains
`epoch`. Stored/native owner_epoch and claim_id spellings remain explicitly
mapped and are not internal compatibility APIs. **Partial closure: O1 names
still owe migration.** No semantic counter defect was established here.

### S6 — exports

Scope/limit/format subclasses own validation, resolution, bounds/truncation and
rendering; current CLI and paired Toad construct actual classes. Removed enum
metaclass, kind enums, string adapters and forwarding factories are absent.
`test_export_families.py:72,84`, export golden fixtures/current behavior tests
and `evidence/export-cleanup/HANDOFF.md` establish extension and installed
export/import behavior. Source fulfilled; no old constructor should return.

### S7 — components, documents and selected transaction

- RuntimeRequest/CliCommand derive socket dispatch/parser from declarations.
  `test_command_families.py:26,63,101,138` covers old help/flags and new real
  command/socket cases. Actual errors use the canonical codec/domain contract.
- CommsAgent composes SessionLifecycle/ConfigOptions/InputDrain/TurnRunner;
  OwnedTurn holds one turn. Old ACP properties/state aliases/dispatch were
  deleted; actual external ACP methods remain composition entrypoints.
  `test_turn_runner.py:219` checks the superseded surface.
- R6 NativeEntry/NativeTranscript decode saved entries once; TranscriptEvent
  variants, replay, page/tail/index/routes and paired Toad use current owners.
  Obsolete kind bags, string role dispatch, transcript adapters and diffs
  negotiation are deleted. TranscriptRoutes imports real saved JSON once and
  drops old live-writer `source` coexistence. No second history index.
- WireLog owns append/durability; Publisher owns publication; Registration owns
  lifecycle. Old MessageBus publication/path/flag forwarding and bus_durability
  module are gone. The fsync/read barrier is retained deliberately.
- R2/R3/R5 close the original residual document targets. FutureInputQueue is
  process-local authority; durable UNKNOWN cannot reconstruct permission.
  Current compaction source checks exclude durably queued future input only
  when that live queue proves it; foreign ingress does not invalidate it.
- R4 GenerationState/GoalAttemptPhase replace raw constructors/status rosters;
  SQL remains GoalAttemptStore's transaction authority.
- R7 SelectedExecution owns selection, lease, source/session/prompt, triage,
  full execution, tools, publication and cleanup. Current ACP/foreground callers
  instantiate it directly; no injected coroutine executor or old procedure.
  R7 source receipts include local actual native four-tool execution and three
  native compaction/queue cases. Parent combined 121-case receipt includes
  actual native four-tool execution. No paid run was repeated for this audit.
- Original post95 OPEN-4 was explicitly reassessed: D1 ManualCompaction and
  D2–D4 journal states/native witness/detached transport closure are integrated.
  Canonical explicit manual `/compact` refusal remains its authority boundary;
  it does not disable default automatic compaction. Issue107 stays issue-only.

These ownership replacements are source fulfilled. Literal size/lock guards,
requested scale benchmark evidence and current installed completion remain
qualified below. This is not permission to replace native locks with shared
JSON reads or split methods solely to satisfy a line counter.

### S8 — goals

GoalState carries paused source/block reason/presentation; GoalAction carries
action parameters/preconditions and derives model capabilities/schema.
GoalExecution derives run/standby from state and real waits; GoalChanged is
authority-produced. Generation/attempt behavior now lives on typed owners;
SQL declarations derive status choices. No Goal legacy constructor, from_legacy,
pause/wait snapshot aliases or repeated pause-source decoding remains.
Saved flat goals and unmatched older pause evidence normalize once at the
boundary without fabricating a turn witness or permitting owner-pause removal.
`test_goal_nominal.py:96,118,162,190,198,213` covers new source/action, all
pause-preserving rewrites, CAS and actual CLI. R4 source and installed goal
set/pause/edit receipts cover remaining generation lifecycle. Source fulfilled.

## Exact deletion closure versus real boundaries

The committed-source script found **zero** tokens for its explicit retired
identifier set and zero retired aggregate imports/modules. This proves absence
only of those exact names; caller/ownership inspection above supplies meaning.

| Mandate | Inspected replacement / deletion |
| --- | --- |
| C0 aggregate/shared-self facade | Comms constructor only; actual components; no operations/declarations imports. |
| ACP149 / InputDrain154 / TurnRunner161 adapters | State belongs to sessions/config/inputs/turns/OwnedTurn; no old ACP accessor block or `_run_agent_turn`. External SDK methods and current effects contracts are real boundaries, not retained old APIs. |
| S1 legacy events | No from_legacy or dict acceptance shim; typed internal events and transcript updates. |
| Runtime145 unknown-extension/error adapters | `RuntimeRequest.from_wire` normalizes the real action key once; FieldCodec rejects unknown fields. Old projection/error-wording hooks absent. |
| Goal134 / pause/wait aliases | Typed Goal/actions/states, direct store read; external flat saved goal normalization retained. |
| Export125 / ResponsePolicy137 aliases | Old kinds/metaclass/factories/upper-case resolve/value adapters absent; actual family classes used. |
| Pi143 / R1 adapters | Old reader/steering aliases and known event Mapping/wire mirrors removed; typed payload/command consumers. |
| S3 state tags/projection forwarding | No state_tags/ClaimState/ProjectionRecord or RecoverySnapshot.to_primitive. Redacted FailedTurnProjection has a distinct real external schema. |
| S5 identity/turn adapters | No ThreadRegistry/TurnClaimFence/ID-only release facade; exact TurnLeaseFence. Current local names remain O1. |
| R2 catalog/message | Old sidecar coexistence and shape caches removed; Message.from_wire uses MessageWireCodec, CLI display forwarder absent. |
| R3/R5 documents | Shared LockedStore reads/updates; typed records, no raw load/save compatibility API; append activity retains a real derived checkpoint. |
| R6 saved transcript/routes | No kind/optional bag, TranscriptUpdate.from_transcript/owner_for, record_input_display forwarding or old-writer source projection. Current paired consumers migrated. |
| R7 selected transaction | run_one_sealed_claim and procedure parameter bundles removed; one SelectedExecution and one DurableTurn fence. |

## Concrete remaining work and evidence limits

### O1 — unfinished R7 naming closure (owner: Darwin/R7; parent integrates)

The original S5 step7 and explicit R7 dispatch require non-resource attention
and turn meanings to stop being called claims. Current candidate retains:

- `acp.py:651,662,670`: **three** callbacks (`check_plan_controller`, `load_plan`,
  `applied_plan`) take `claim: WakeAssignment` and pass it onward.
- `owned_turn.py:204,830`: actual `self.turn_claim` stores/passes TurnLeaseFence.
- `turn_runner.py:189,198,380,393,408`: relay local and settlement parameters
  still say `turn_claim` / `claim: TurnLeaseFence`.
- `turn_progress.py:318,412`: current field accesses and `claim=` keyword.
- `manual_compaction_bridge.py:59,126`: turn lease local named turn_claim.
- `goal_failure_observation.py:52–84`: exact lease proof parameter named claim.

Complete current caller/test migration should rename these to assignment/lease,
including FailedTurnObservation.from_terminal's keyword. Do not rename resource
claims, goal launch grants, stored claim_id or native authority encodings.
`source-inspection.json` records29 exact NAME occurrences in these source files;
this count is a locator, not a new task count. **No code edited in this read-only
assignment.** This is not evidence of stale signaling or duplicate execution.

### O2 — literal plan guards remain incomplete (owner: parent original-plan reconciliation)

- S7 step9's universal module<=1000/function<=100 guard is false: seven modules
  exceed1000 and53 functions (including nested closures) exceed100 lines.
  Exact original-scope witnesses include SelectedExecution._send_boundary
  (`coordinated_runtime.py:1015`,250 lines) and OwnedTurn.send_boundary
  (`owned_turn.py:274`,276 lines). They are real admission owners. Their length
  does not establish an independent missing mechanism; record the unmet guard
  explicitly instead of declaring every original acceptance item passed.
- S4 step5's no-built-in-literal guard is not literal truth: e.g.
  `turn_runner.py:58` and `claim_admission.py:202,244` still use `"#all"`.
  These select the existing global target, not an alternate alias registry.
  No user-visible routing failure was established. Parent owns recording whether
  these remaining constant references are folded into existing source cleanup.
- S7's no `_store_lock`/`_atomic_write_text` outside A8/WireLog is also not literal
  truth (e.g. outer turn wire fence and MessageBus executor acknowledgements).
  A8 explicitly excludes the wire; original OPEN-3 requires preserving the
  claim-gate durability barrier. Do not weaken required outer/native/SQLite
  authority locks to make a grep green. Acknowledgement is delivery progress,
  not a second human ReadLedger. Prior original audit already recorded this
  guard qualification; it must remain visible in final status.

These are original plan acceptance discrepancies, not a duplicate new-debt
census. Pascal owns new post-feature debt analysis separately.

### O3 — evidence is substantial but not the original exhaustive matrix (owner: parent acceptance record)

- S1/S2 T4 requested recorded real streams through old and new consumers across
  every listed case. S2 has ten deterministic old/new fixture scenarios; actual
  local native and configured-provider paths have other receipts. They are not
  the requested complete archived real-stream equivalence matrix. No such full
  matrix was established by the inspected handoffs; do not invent one or rerun
  paid providers to simulate historical characterization.
- S7 requested before/after50/100/150 threads with three pollers, send p50/p99
  and lock hold/wait timings. Existing bounded/index tests, R5 activity timing,
  installed UI samples and parent's schema reflection profile are different
  evidence. The requested complete benchmark is not established here.
- New-case tests are concrete for A1/A2, event, phase/failure, coordination,
  response policy/view predicate, export limit, commands and goals (references
  above). This does not certify a saved before/after edit-count experiment for
  every original noun, or a reconstructed chronology for every T3 xfail.
- CI/full-suite historical gates are explicitly superseded by owner local-test
  authorization. This audit requests neither CI waiting nor broad repeats.

### O4 — installed/live completion (owner: parent; already in progress)

`evidence/r6-integration/HANDOFF.md` at ead4800 now records combined121cases,
installed process/tool/context/channel-reader checks, and combined mounted
copied #comms20/#nra8 rows,111 saved identities and a saved transcript without
bus changes. The full original route comparison preserves59465 routes,
5617 displays and5408 bindings, plus current11 display/binding records. R6 also
preserves bounded latest/prior facts and cursors across all88 saved sessions.
These are stronger than a source-only candidate and are already accepted;
this audit does not rerun them.

The same handoff still explicitly leaves final installed configured-provider
queue/compaction, pins and normal coordinated activation/backup/live verification
to parent. Last verified historical live receipt is core199/Toad97 R3 with
103 identities/bus60/checkpoint60, four-fact once/in-order queue behavior and
2580 UNKNOWN records preserved. It cannot certify R6/R7 activation. R6 drops
routes.source: all old writers must exit through the existing normal coordinated
restart before migration/new writers; no replay or fabricated receipt.

## Handoff

No missing major original ownership component or new functional failure was
established beyond O1's concrete incomplete vocabulary requirement. O2/O3 are
real acceptance qualifications; O4 is already parent-owned work, not a fresh
hold. Keep original-plan status qualified until these entries have explicit
dispositions and parent records current activation. Do not label the whole goal
complete on source/NRA results alone. No new provider call, test suite, agent,
model, live mutation or new implementation task was started for this receipt.
