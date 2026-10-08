# Retain the inbox's original compaction refusal

## What failed

`openhcs-pr159-viewer-bind-owner` has a stopped-drain diagnostic in the original
`/var/tmp/agent-comms-live-20260927-wzjtqhza/activity_latest.json`:
`CompactionJournalError: Selected source requires reviewed raw-history coverage floor`.
The original owner log contains no traceback for this refusal. No original
diagnostic document contains its chained exception.

The complete production call relation is:

1. `InputDrain.observe` runs the private inbox through `CommsAgent._drain_private_nk`
   and the existing selected turn/owner compaction path.
2. `OwnerCompactionCommit` owns the acquired native reader and captured source.
   `CompactionBoundary`/`HeldCompaction` capture owner, ingress and native revision;
   the live future queue selects the original input view.
3. `SelectedSummarySlot.run_selected_summary` calls `SelectedSummaries.reserve`
   before writing a selected-summary command to the native child.
4. `PrivateInputs.require_source_coverage` finds `SessionJournalHistory` for this
   saved file and calls `verify_continued_private_session`. Its exception wrapper
   supplies the recorded outer message and retains the cause in Python.
5. That verifier requires the reserved revision, strict native evidence, original
   input dispositions, recorded fork/compaction prefixes and the coordinator's
   original `NativeRuntimeInput` context receipts. `NativeContextJournal`
   independently corroborates receipt identity and generation. Recorded anchors
   cover ancestry; they do not settle an uncertain managed input.
6. `InputDrain.observe` classified the wrapper as a stopped drain, but passed only
   its type and string to `AgentActivity.set_drain_diagnostic`. This discarded the
   chained exception before the original activity event was persisted.

The journal has 70 prewrite markers for this saved file, which is why coverage is
required even though the file is outside the journal's native-session namespace.
The marker label `unknown` is not itself a denial: continued-source coverage joins
it to original live receipts. A read of the existing data found all 70 coordinator
receipts equal to their exact native journal records, and their native entry IDs
and session identity agree. The 21 recorded started-input texts agree with saved
user messages. Recorded ancestry covers all untracked user entries. There are no
selected-summary rows or compaction-operation rows for this file in this journal.
None of these records was changed or promoted.

These reads are not the original failed cut. The chained exception, reserved
source revision and acquired reader state were never persisted; reservation
failed before inserting a summary row. Current agreement cannot distinguish an
original read/decoding/schema/revision refusal. The original first cause is
therefore unrecoverable from the retained evidence. No missing history, participant
conflict or transient contention is claimed as its cause.

## Repair

Both existing `InputDrain.observe` failure branches pass the actual exception to
the activity owner. `AgentActivity.set_drain_diagnostic` checks the original owner
fence, durably retains the exception chain, then publishes the diagnostic's private
file location in the existing human-facing `reason`. No separately decoded path
member remains. Availability, stop/propagation and clearing behavior are unchanged.

The existing diagnostics module shares exception serialization and durable writing
between terminal and inbox failures. Traceback locals are excluded. The inbox file
name derives from the original failure document: identical observations share a
reference; different chained errors retain separate files even with the same outer
message. Activity exposes the reference, not the private traceback. No new turn,
summary, receipt, enrollment, retry, background task or recovery store is created.

Production declarations and calls were read across src/tests/tools. AST parsing
and compilation covered 757 Python modules with no omissions. Dynamic FieldCodec
family dispatch is carried by the existing shared declarations, not resolved by
the static call enumeration. Existing fixture callers that have no real exception
retain their existing diagnostic-only behavior.

## Confirmation and limits

Two focused source controls passed. The coverage control uses the original
continued-session fixture, journal and real filesystem refusals. It verifies full
causal traceback retention, private file mode, changed-cause separation, identical
observation deduplication, stale-owner refusal, original-format codec roundtrip and unchanged
session/input bytes with no summary inserted. The second control checks decoding
an original diagnostic with no private reference. No mock backend or event stream
was supplied. Initial local check setup errors (pytest xdist arguments, absent
scratch parent, omitted fixture tags and an incorrect ActivityLog path accessor)
were corrected; they establish no product or native result.

This is a source repair, not an installed inbox recovery. No original input or
compaction was retried, no native process or App was started, and no package was
built, installed or rebound. Integration can use this change to retain the exact
cause on future independently authorized work; it cannot recover a lost traceback
or authorize replay of the consumed failure.


## Same-format correction and delivery

The earlier optional path field had no behavioral consumer. Complete src/tests/tools
references found only its declaration, publication, artifact normalization and the
new source checks. Toad production has no path consumer. Original consumers acquire
Activity through ActivityLog/FieldCodec, then owner-fenced DrainReadiness, ThreadView,
thread/history presentation and notifications. They use the existing diagnostic
summary/reason; no consumer parses a file path. The path field has been deleted.

AgentActivity retains the original chained error through record_drain_failure,
then exposes the returned artifact location in the existing reason string. The
artifact derives its original outer reason from source_error, so giving the owner
an already-presented diagnostic does not duplicate the reference or change its
artifact identity. There is no path parser, second registry or alternative codec.
Different causes still produce different immutable files and visible reasons.

The final activity.py, field_codec.py and activity_checkpoint.py are individually
byte-equal to BOTH installed backend runtime and frontend runtime-send-focus Core
copies under the sidebar-live-candidate deployment. Original Activity/DrainDiagnostic
fields and checkpoint schema are unchanged. Both existing affected controls passed
in one batch: 2 passed in 0.36s, system Python/pytest, private checks06 scratch.
The first roundtrips the newly presented real refusal through this original decoder;
the second asserts the exact original diagnostic keys and legacy roundtrip. These
are the old-reader declarations themselves, not a fabricated compatibility adapter.

The broad reader-cohort cutover and incompatible-rollback requirement described at
c43064dd are superseded by this correction. Old readers can read future activity
rows without updating their Core. Installing the failure-preserving backend code
still requires a truthful wheel/source proof through existing deployment owners;
no build, installed result or unchanged latest-main source equality is claimed.
Old workers lack exception preservation but do not lose activity decoding when
updated workers publish these same-format rows. Rollback readers retain format
compatibility, and private diagnostic artifacts remain preserved.

pr159 is untouched. Its original coverage refusal remains unresolved; original
receipts agreeing now cannot recover the lost first cause. No stop, restart, drain,
input, summary, native process or replay was performed. The previous recovery trace
identified automatic wake on ordinary worker restart and pending source work; this
schema correction does not authorize restarting it or settling uncertain input.

## Runtime attachment failure preservation

The actual UX log at `.local/state/toad/logs/Agent_Comms_2026-10-06T22_08_43_325438.txt`
contains initialize and session/load, identity metadata, then the busy-read failure;
no prompt was submitted and no current-admission input was recorded. Subscription
runs identity publication, transcript capture/replay, turn and input-ledger notices,
configuration and ready metadata in order. The failure preceded the snapshot update.
Transcript capture can read both compaction outcomes and coordinator reply records.
The old socket error alone cannot distinguish those stores or identify its writer.

RuntimeServer.handle is the caught-failure owner for every decoded runtime request,
binding and apply operation. It now logs the original non-cancellation exception
chain before `_owner_error` reduces it to the unchanged socket error. Original
owner_process_output already directs stderr to a private retained owner log.
Cancellation, response serialization, subscriber removal, permission denial and
socket retirement are unchanged. No new diagnostic file or wire field is added.
CoordinationStore.observing adds store path, original timeout and SQLite name/code
to the original OperationalError as exception notes before the existing busy
classification. The traceback preserves the failing SQL call and consumer chain;
notes are private exception evidence, not response text or retry authority.

No OS lock-holder capture was added. SQLite raises after its busy wait has ended;
Python exposes neither the conflicting SQLite descriptor nor blocking owner.
The observing boundary then rolls back/closes its read before classifying failure.
A later `/proc/locks` sample cannot prove it contains the transaction that blocked
that original read, and SQLITE_LOCKED can be internal connection contention rather
than an OS lock. The missing observation is the conflicting lock/transaction while
the original read is actually waiting. This change retains the first concrete
store/statement failure; it does not guess or patch the historical locking cause.

Two focused source checks passed together (1.37s). The added check uses a real
owned Unix runtime subscription and the original private compaction journal's
exclusive transaction. It verifies exact unchanged busy response, full causal
traceback including the snapshot SQL/store, no ready/controller, subscriber cleanup
and no input/native session. The existing store cleanup/schema-disposition check
also passed. Initial check setup refused missing system ACP dependencies and an
absent scratch parent before test entry; retained dependencies and a persistent
owned scratch parent resolved those setup issues. No dependency install occurred.
These are authored private source checks, not an installed UX retry or contention
repair. Neither UX nor pr159 was restarted, attached, drained or given input.

## SQLite snapshot lifetime correction

The completed source pass found unnecessary CPU work under original read locks.
RecoveryGateway acquired its entire RecoverySelection and then encoded the wire
response inside CoordinationStore.observing. CompactionJournal.retained_history
serialized each captured row inside the all-table read; retained_changes encoded
identities and compared complete retained payloads inside the two-attempt read.
WorkingMemoryAnnotations grouped acquired segment answers and evaluated calibration
cases inside its read. These operations consult captured declarations only; none
performs another SQL read, admission or publication.

Those existing owners now finish the original complete snapshot before encoding,
inspection, comparison or evaluation. No query or validation needed to acquire that
snapshot moved to another transaction. Calibration captures the original model
answers and every corresponding correction family together before evaluating them.
The journal still reads every declared table together and verifies original custody
before returning rows. Missing journal, insufficient cuts, response size refusal,
UNKNOWN history and exact result shapes remain unchanged.

The shared TypedRow/TypedTable boundary was traced but not changed: iterate owns a
streaming cursor; eager read/joined close their cursors, but explicit transactions
belong to their callers. Decoded declarations are needed for subsequent predicates
and multitable checks. Ending a transaction from a row decoder would split those
snapshots; fetching all raw JSON rows first would not release an explicit outer
transaction and would duplicate retained payload memory. FieldCodec is unchanged.

Production AST trace covered all 757 src/tests/tools Python modules without parse
omissions. All 92 lexical read/transaction/irreversible_admission scopes contain no
await. This alone does not prove arbitrary callbacks are free of blocking work.
The relevant source families were read semantically: CoordinationStore observes
BEGIN through rollback/close; CoordinationSession reuses an enclosing write for
read and owns write commit/rollback. PrivateSendAdmission commits its exclusive
admission and durable input disposition before raw prompt dispatch. Response
publication intentionally keeps the bounded bus append/fsync with its fenced
receipt transaction. Their atomic publication relations were preserved.

Saved transcript acquisition captures CompactionOutcomeSnapshot with all matching
summary/commit decisions in one journal read. Source-cut checks happen after that
read. Native fragment projections collect original admission and publication
receipts in one coordinator read; rendering happens afterward. Runtime transcript
replay awaits a worker result, not an open SQL context. Continued private source
coverage consumes its original journal and native evidence fences; no coverage or
recovery decision changed. Optional awareness already releases SQL before registry
inclusion checks and digest/encoding. No independently changing owner was merged.

Consumer trace: SnapshotInvocation.run uses the one gateway snapshot; retained
context CLI and CompactionBoundary use the original journal inspection/diff owners;
annotation CLI calibration and runtime segment/context callers share
WorkingMemoryAnnotations.for_segments. No per-consumer alternative was introduced.
The separate native_pi inner _decode_row is unrelated to TypedRow. Dynamic caller
behavior is not inferred absent from the lexical scan.

Three existing private controls passed after instrumenting their actual stores:
recovery snapshot/one metadata read, original answer/correction/calibration, and
retained-source inspection/diff/export. Independent real SQLite writers acquire
exclusive locks during annotation evaluation and journal inspection/comparison.
The gateway's real participant-generation writer waits while projection reads its
old snapshot, then commits before encoding; the response still names the captured
old owner and the next acquisition names the new owner. Measured read lifetime was
7.509ms; writer finished 3.950ms after release. These are one private observation,
not a live latency benchmark or identification of the historical blocking writer.

Raw checks: `/home/ts/.cache/agent-scratch/mendel-sqlite-read-lifetime-20261006/checks.log`
and `gateway-corrected.log`. The first batch had two passes and one instrumentation
failure: the FieldCodec observer omitted its annotation argument. Corrected observer
preserves the complete signature; only that failed check was rerun (pass, 0.66s).
No dependency install, live attachment, UX/pr159 restart, drain, input or summary
replay occurred. The earlier blocking writer/store/statement remains unproved;
6cee7b71 preserves that causal traceback for an actual future failure. This patch
removes proved unnecessary lock lifetime without claiming that historical cause.

## Native ancestry outside the attach read

The actual UX load emitted identity/blocked-goal metadata and failed before Ready,
with no prompt. Its current owner stderr descriptor names
`/var/tmp/agent-comms-live-20260927-wzjtqhza/diagnostics/owner-66e20696e728cdbd5ca8657e83697130-d92d2112ae8c4f3cb6321482500bcecb.log`:
29 bytes, only the launch line, no causal traceback. Current source diagnostics do
not establish what this running worker loaded or identify the historical writer.
RuntimeProxy.subscribe -> SubscribeRuntimeRequest -> TranscriptReplay ->
TranscriptSnapshotUpdate capture reads both journal outcomes and coordinator reply
relations; either observation can export the same reduced busy error.

Proved source defect: AssignedTranscriptSource.native_records held the coordinator
read while walking native parent records and repeatedly decoding the session header.
NativeTranscript.input_for now owns input/parent selection for transcript and final
publication consumers. Bounded-fragment ancestry is acquired before SQL. One header
lookup remains inside the selected read, preserving detached-history behavior when
the coordinator is absent; for_native_user receives that captured ID. The separate
publication_revision retains its existing single header lookup. No identity cache,
new state, schema or codec was introduced. Input/stage/execution/reply queries remain
one atomic fragment snapshot; rendering and wire corroboration still follow close.

Read/write lifetime and format consumers were inspected: CoordinationSession,
CompactionJournal, recovery gateway/projection, native publications, notification,
continued coverage, optional awareness and working memory. Required reservation/raw
exclusion remains; PrivateSendAdmission closes its grant before pipe bytes. A shared
reader can delay a pending writer commit, which can deny later readers; this is a
possible relationship, not the historical cause. AST inspection covered 758
src/tests/tools modules with zero omissions, including partial callback references;
external dynamic callers remain outside that enumeration.

Header preflight remains an unresolved requirement collision. Raw DB-header closes
can release concurrent same-process POSIX SQLite locks; Python exposes no managed
header-only inspection that refuses WAL before sidecars and respects changing data.
Immutable suppresses locks/recovery/change detection and journal_mode reports pager
mode, not the required raw header answer. No workaround was added.

Three focused private source checks passed. The real zero-timeout coordinator writer
committed during original ancestry acquisition before the read opened; one header
lookup and exact events/native IDs were retained. Original bounded decoding and
malformed/partial traversal passed. Two fixture mistakes (assuming generation1 for
every callback, then omitting the original native_id) are preserved in raw logs;
only the failed check was corrected/rerun, with no production change. Evidence:
`/home/ts/.cache/agent-scratch/mendel-attach-read-lifetime-20261006`.
This is source ordering, not installed UX recovery. No restart, attach, input,
compaction, provider, prefix mutation or build occurred. The original failed SQL
statement and blocking writer still require a causal traceback from an actual
failure; the lost historical evidence cannot be reconstructed. UX/pr159 uncertain
work remains untouched.
# Context manifest acquisition — 2026-10-07

The installed context panel reacquires on each global observation. The writer's
ContextManifestSources index already selects only the requested incarnation and
its retained aliases. The expensive repeated work was decoding those same
original observations, not choosing unrelated wire rows.

CertifiedSourceRead now lends CapturedWireSource resources: the original
PrefixSource, pointer, bytes and decoded value. `WireLog.context_manifest_resources`
selects the current certified source and current registry membership on every
call. Passing the previous inspection's resources reuses a decoded value only
when physical/logical source, typed pointer and exact original bytes agree. New
observations decode individually. Reused manifests still resolve against the
new registry snapshot. No global cache, signature, durable field or currentness
waiver is introduced. Generic capture_sources shares this capture implementation.
WritableAccess owns indexed acquisition; ArchivedAccess retains the original
strict WireScan and its distinct archive certificate, without trusting a later
observation index. Archive scans are not optimized by this change.

Toad handoff: add `manifest_sources: tuple[CapturedWireSource[ContextManifest], ...]`
to the existing ContextInspection. Its read method should accept the preceding
inspection (or its resources), call
`comms.bus.log.context_manifest_resources(owner, comms.registry, previous=...)`,
and derive manifests from resource.value. HoldingInspection acquisition must
supply its own retained resources through its existing polymorphic lifetime;
unacquired states supply an empty tuple. Do not put a second signature in the
widget. Keep the original registry.require, SessionRevision.observe,
WorkingMemoryAnnotations.for_context and ImportedSessionMetadata.sources_for_owner
reads. Those independently own current process/incarnation/name/model/thinking,
selected SDK file, effective annotation/correction and imported provenance facts.
NativeContextData.with_current_contributors remains the current preview owner.
Publication identity and SDK revision retain the existing same_native_source
decision; manifests retain original recorded request IDs and provenance.

One authored real private-store check: 20 originals decoded on first acquisition,
zero on repeated acquisition after another owner's append and after rename, one
on the next selected append. Original resource object identity is retained.
Tampered wire bytes still refuse even with previous resources supplied. Together
with the existing rename/predecessor/incarnation check: 2 passed in 1.29 seconds.
The first invocation stopped in tmp_path setup because the named scratch parent
did not exist; neither check entered. The corrected invocation created the owned
scratch parent and used a fresh destination. No runtime or installed application
was launched. Selected bytes still have to be captured and compared on every
acquisition; this removes repeated JSON/FieldCodec decoding, not all history IO.

AST source trace: 758 src/tests/tools modules, zero parse/compile omissions,
66 related declaration/call sites. Both WireAccess implementations migrated;
public context_manifests consumers keep their existing tuple answer. Runtime
recorded-source requests and CLI diff still consume those original manifests.
External dynamic consumers are not resolved by the AST trace. Trace and check
output: /home/ts/.cache/agent-scratch/mendel-context-resource-reuse-20261007.
Toad integration and installed panel CPU verification remain Parent's next step.

## Implicit project actor command discovery — 2026-10-07

The retained streaming App traceback reaches TargetContext.available_actions,
CliCommand.target_catalog and RegistrySnapshot.require('project'). A view actor
does not itself declare a backend thread. Catalog discovery now selects thread
members through its already-acquired RegistrySnapshot canonical namespace before
asking command declarations for bindings. Channel bindings are unchanged. There
is no reserved 'project' spelling: a genuinely declared thread with that name
still exposes its actions. Execution retains its original strict fresh lookup;
an absent target cannot become executable by having appeared in a view.

The new real private-store check passes for absent actors, strict execution
refusal and a genuinely declared project-named thread. The existing mixed
missing/present batch check also passes, including truthful partial outcomes.
Raw checks and AST sites are retained under
/home/ts/.cache/agent-scratch/mendel-project-target-catalog-20261007.
The first invocation refused unsupported repository xdist options before entry.
The initial new test incorrectly tried renaming a stopped thread; its failure is
retained, and the fixture now uses the original stopped declaration operation.
Only that failed check was repeated. All src/tests/tools Python parsed, with 23
related declaration/call sites and no omissions. No installed App, native input,
publication, build or restart was performed; Parent carries integration.

## Viewer pending/sent acquisition — 2026-10-07

The original installed channel profile records 29 viewer_snapshot calls taking
34.934 seconds inclusive. Its 29 pending_counts calls took 5.584 seconds and
MessageBus.last_sent_timestamps took 3.513 seconds (HistoryViews canonicalization
inclusive 3.832). These are original overlapping inclusive costs, not additive
or a measured speedup of the correction.

BusPresentation.snapshot already lends an OpenedWireSnapshot and DisplaySelection:
fixed bus byte cut, registry incarnation/aliases, catalog membership and selected
viewer read ledger. HistoryViews nevertheless called pending_counts and
last_sent_timestamps independently, acquiring two further bus cuts and fresh
registry/catalog/read documents. The resulting sidebar could also combine answers
from different appends or acknowledgements.

MessageBus now shares the existing delivery counting implementation through
_pending_counts. Its ordinary pending APIs keep their original revision cache;
pending_counts_opened takes the actual opened source and DisplaySelection and
uses their original registry/catalog/seen relation. DeliveryScope still derives
actor/channel membership and rejects sender/recipient reincarnations; route index
sync and unread queries use the captured byte cut. Disposable index refusal falls
back to the same strict WireScan bounded by that source, not a fresh acquisition.
No human display scope, activity clock or transcript cursor substitutes for the
delivery answer. Captured counts do not publish a named-file revision cache.

The existing BusActivityIndex accepts the same opened source for sent clocks.
HistoryViews canonicalizes through the captured registry rather than rereading
it. viewer_snapshot feeds both projections from its existing presentation
lifetime. Its transcript file IO remains after that source closes. The unused
WireLog.projection_snapshot tuple wrapper is deleted after migrating both actual
consumers to the existing OpenedWireSnapshot; strict scanner ownership remains.
No cache/index/protocol/schema/Toad member or backend mechanism was added.

One focused real private-store batch passed all 18 viewer/current-delivery checks
in 12.56 seconds. A new check observes exactly one opened/closed bus cut per
viewer_snapshot, with genuine direct delivery, channel display unread and sent
clock answers. Another keeps its original pending/sent answers across actual
later append, sparse acknowledgement and rename, then obtains the new answer
on the next snapshot. Both actual indexed storage and a real unavailable
SQLite index path pass. Existing reincarnation, sparse read, scope, corrupted
index, append-boundary and strict uncertified source checks remain passing.

All 759 src/tests/tools modules parse/compile; 461 before/after related AST sites,
zero omissions. No projection_snapshot consumers remain in that source family;
external dynamic use is not established by AST. Raw checks, source trace and
original profile costs: /home/ts/.cache/agent-scratch/mvp01/check.log and
source-family.json. One initial shell edit was refused by the duplicate-command
hook before execution; the source changes used apply_patch. No App/public input,
provider/native process, prefix operation, build or restart occurred. Parent owns
integration and actual installed latency measurement; no live speedup is claimed.

## Current claim marker acquisition — 2026-10-07

Parent delivered the viewer-cut correction and supplied a new installed profile:
`/home/ts/.cache/agent-scratch/installed-viewer-cut-header-20261007/ui.pstats`.
46 viewer snapshots take 18.106 seconds inclusive; pending_counts_opened takes
0.889 seconds. Different workloads and overlapping/threaded cumulative times do
not establish a speedup or UI blockage. Marker acquisition/source certification
remain substantial; FieldCodec.decode is 1.499 seconds across the entire profile,
not evidence that it alone explains the viewer cost.

RegistryStore already caches decoded registry documents, but private_guard_unlocked
must verify the marker and original committed guard before every cache hit. These
are independently changing authority facts. That check remains untouched.

A concrete duplicate exists inside WireLog.verify_before_read_unlocked:
claim_gate_enabled decoded the current marker, then the barrier decoded it again
before any publication/durability work. Canonical writers hold the original bus
lock; neither intervening gate check writes a marker. WireLog now derives the
boolean gate and barrier's typed marker from _claim_marker_unlocked. The barrier
carries that acquisition rather than repeating its decode. Nonbus locks, missing
claim/certificate refusal and owner-only inode admission retain their exact checks.

opened_claim_source_unlocked separately samples _private_marker_unlocked AFTER
fsync. That is a genuine durability/currentness boundary and is preserved. Later
_certified_source and opened-reader marker relations also remain current. Total
claim barrier samples are three to two, not two to one. No cache, retained
signature, format, timeout, registry bypass or public operation was added.

The two affected original private-store checks passed in the first batch. The new
observer check incorrectly expected one sample and failed: it had omitted the
post-fsync owner from its expectation. Production was unchanged; only that failed
check was corrected/repeated, passing in 0.58 seconds. It observes actual marker
reads across two real private publications: one gate sample and one post-fsync
sample each, with correct root/current sequence and certificate. Original
addressed-source completeness and edited-prefix rejection checks remain passing.
Raw check.log/check02.log and 340 before/after owner/writer/consumer AST sites are
retained under `/home/ts/.cache/agent-scratch/mmr01`. All 759 modules parse/compile,
zero omissions; external dynamic overrides are not established by that trace.
No build/prefix/App/provider/public input/restart or repeat installed check occurred.
Parent owns integration and measurement; no remaining historical writer or
deadlock cause is inferred from this profile or source change.

## Complete compaction context ownership correction (2026-10-07)

The current UX diagnostics `9767127f2bde4a3a8adece454e83bd28.json` and
`7280a266a255f58f51e7833564ec5acd.json` under the original live route both
preserve native `Compaction result exceeds its selected context budget`.
The reached boundary is OwnerCompactionCommit.prepare_source's second native
preparation, before a selected summary reservation or provider request. The
earlier decoder failure is separate; upgrading its decoder exposed this refusal.
Neither failed input nor any original compaction was replayed.

### Required answers and existing owners

RetainedTaskFacts owns captured task facts. Its full codec representation, text
inspection/export, original input rows, content checks, source membership and
journal framing remain unchanged. Wire human messages, direct human input rows
(including historical instructions), task decisions, goals and native artifact
facts keep exact wording. HumanInputTaskFact supplies this behavior polymorphically;
current/historical scope never authorizes deleting that wording.

Neutral InputTaskFact rows include the previously rendered native prompt, not a
second human instruction. OwnedTurn.begin records ScheduledTurn.incoming.prompt
in those rows. The original wire facts/native conversation already supply that
content. InputTaskFact now supplies a one-way compaction projection with original
provenance, content digest and public disposition; the full row remains frozen in
the journal. This deletes the decision that all durable delivery evidence must
be repeated verbatim inside every compulsory summary prefix. No durable field,
decoder, source coverage rule or input admission changes.

OwnerCompactionCommit's allocation, SelectedSummarySlot's request and
RetainedTaskFacts.require_summary all take this same compaction_text. Full source
equality is still checked before reservation/commit, including original input
content and pending input keys. SelectedSummarySource.journal_json still frames
the complete FieldCodec.encode(retained); NativeIntent still binds the canonical
summary payload and metadata digests. Recovery retains its original identity,
fsync, terminal and no-replay requirements. The model projection cannot become
an original input or recovery receipt.

CompactionPolicy owns context allocation; ContextBudget owns token accounting
and capability admission. SessionContext supplies the actual system prompt,
converted messages and tools. Native preparation, suffix allocation, pre-provider
mandatory packing, generated-result packing and final committed-context admission
now all consume that original session. The session is passed to compact, not
stored in CompactionPreparation or exposed through its extension hook payload.
Manual and automatic paths share AgentSession._runDefaultCompaction, which passes
this; both preparation callers pass this. Selected preparation/repreparation and
selected generation pass their already acquired session. The exported declarations
match. No system/tool copy or retained budget signature is introduced.

The native estimator invalidates older assistant usage after a newer prefix
timestamp. Packing creates the new summary envelope with Date.now(), so its full
system/tools/messages estimate cannot reuse the old pre-compaction measured prefix.
Existing selected model, reserve, policy ratios, atomic suffix rules and complete
file annotations are unchanged. A mandatory payload that genuinely does not fit
still refuses. No required human wording or file ledger is shortened to force it.

Map/reduction sources and synthesis are genuine different requests. Their existing
SummarySource, CompactionPlan and buildSummarizationContext request admission stay
separate from the conversation that will be restored. Provider generation allowance
is not the retained result size. Prefix-capable generation still uses its original
provider/converter/context hook constraints and current source fence.

Offline EntryStore previews have no acquired system/tool owner. Their explicit
message-only result does not qualify installed session admission. Three existing
source controls/inspection consumers still called deleted inputBytes/messageBytes
APIs after the token migration; they now take inputTokens/sourceTokens/contextTokens,
with token-labelled output. Historical evidence is not rewritten. Real runtime
callers pass the session; external extension behavior is not proven by a static trace.

### Prior corrections and why this refusal remained reachable

The ancestral source changes fall into the following owner relationships. Branch
duplicates `fd250c74e`/`4ce8b3e2c` and `5a69655ac`/`9e015800c` carry the same early
policy/output work. The accidental deletion/restoration `1ca956284`/`7d242186a`
does not supply another budget policy. Original diffs are retained in scratch.

| Changes | Actual correction | Remaining distinction |
| --- | --- | --- |
| 4ce8b3e2c, 2177f0b3a, 9e015800c | Shared native map policy, prior/split-turn summary and output bounded by reserve | Generation output and durable required context remained different budgets |
| 37796d50f, dadb7c495, 9aef5b8c7, 45cf84218, 351a4930b, 2ebefe8da | Capture before provider, actual settings/trigger, managed admission and typed metadata | Correct source custody did not decide which captured evidence was compulsory model text |
| 09463db70, 872fb075f, 47c8e70d6, b74774f5b | Original native intent/CAS, file operations/usage, durable pre-request reservation | Early size caps existed on framing and file metadata, separate from context admission |
| 9c47f42ef, cb5eb5968, 052581ff6, b102fdab8, d4e931306 | Existing transport, typed native responses/recovery and journal transaction owners | Storage/transport identity correctness did not imply a fitting provider context |
| 75c8364df, b20c8cf7d, 63415f8b9, d42742514 | Remove duplicate source/file caps; indexed streaming source, CLI/history/branch consumers | A large source could be acquired safely yet still be copied into mandatory context |
| 224cf66e5, c9777c268, 1f6bc3895, 23284c8f3 | Cold native decision, exact committed cut, token rather than byte admission, full restored session context | Preparation and packing still counted messages without original system/tools |
| 4c69b7027, 79c379ca8, d82316aa7 | Delete dry-run/builder duplicates and repair native source/declaration boundaries | These removed competing paths, not the compulsory evidence/context mismatch |
| d1f7098ad, 237c70014, c4ceec424, 51e479d0c | Native progress/accounting and bounded rolling/shared source orchestration | Scheduling/accounting cannot make a mandatory payload smaller or change its meaning |
| 51bbb958b, 5c5792c95 | Explicit pre-stream provider rejection negotiation and requested allowance versus capability | Known provider rejection is not permission to replay an uncertain input; no effect on this pre-provider refusal |
| 547ea98f9 | Exact task facts and narrative-only packing | Full captured neutral input rows became compulsory text, including prior rendered prompts |
| fe450f655, 153e999e5, 29df4c810 | Canonical retained payload framing across reservation, intent and recovery | Removing transport caps correctly preserved evidence but did not distinguish its model projection |
| 6c24cb66d, 0a0025b21, f46f1b5c4 | Token-based source allocation, map/synthesis wrappers and generation intent versus retained context | Second preparation still received full journal text; packing still omitted system/tools |
| 7e199fa23, 02b0eea42, 9d42ff50e, e88f332d1 | Original prefix provider, original admission/acquired store and ContextBudget-derived observations | Correct acquisition/observations exposed the refusal without repairing those two meanings |
| 2eac51dfe, 28c08fc43, b9d953c72, 6315b56cf | Exact input content, native artifact facts, human provenance and input constraints | These facts remain authoritative; projection must not remove original human constraints |

The recurring structural error was treating three different answers as one size:
durable evidence retention, generation request/output, and restored native context.
The repair changes their existing owners and complete runtime consumers rather
than increasing one path's cap or shrinking only its recent suffix.

### Source checks and remaining application boundary

Existing Package AST acquisition covers 324 src, 381 tests and 54 tools modules,
zero Python parse omissions; before/after owner sites are retained in
`/home/ts/.cache/agent-scratch/mfc01/retained-owner-{before,after}.json`.
Native JS consumers/imports and SDK declaration/call relationships were read
directly; node syntax checks passed. An independent JS AST enumerator was not
available; dynamic external extension behavior remains unqualified.

The two original real private-store retained-context checks passed together in
2.58 seconds. They check full journal/export equality, omission of neutral rendered
prompt text only from the model projection, provenance/digest/disposition, strict
summary-prefix admission and retention of both original human rows with equal
wording. Raw results: `/home/ts/.cache/agent-scratch/mfc01/retained-projection-check.log`.
The existing unrelated codec optimization remains outside this checkpoint.

Native syntax and unified patch parsing passed; shared manual/auto call and compact
declaration were checked against original published SDK source. No native imports,
provider request, compaction, input, build, artifact mutation or live worker restart
occurred. This is an implemented source repair, not a demonstrated live fix. Changed
native deployment source requires a new truthful native commitment/consuming wheel
before genuine isolated saved-session compaction and resumed-input acceptance.
The immutable 4b runtime cannot be labelled source-equal to this repair. Original
failed cuts, both diagnostics, input uncertainty and all receipts remain preserved;
current inspection totals do not establish the exact payload of those failed cuts.

Native artifact preparation completed through `prepare-pi-native --update-pins`.
The existing native_package owner now writes its existing diagnostic/tree manifest;
normal builder verification and existing-target refusal remain. One stock copy
reused the verified original MCP subtree, with no resolution/provider/runtime run.
Package: `stack/.pi-native-062ef93941fde89e/node_modules/@earendil-works/pi-coding-agent`.
Manifest: `062ef93941fde89ea1c58b245ba918d28a9ae807a483fbf564c8db9d4781576f`;
tree: `e65cd9ca7065db03807b81fb11b775a89f3b4448373b4c282b41248cf0ce44a8`.
All 21,276 nodes/19,190 files preserve old4b membership and modes. Only the six
expected agent-session/compaction policy+implementation+declaration/session-context/
RPC files changed. Old4b's original tree still matches; the new files are sealed.
Assembly log and exact comparison: `/home/ts/.cache/agent-scratch/mnc01/prepare.log`
and `package-relation.json`. Parent owns consuming runtime build/publication.
The affected original configured check is
`tests/compaction_source_successor_installed_journey.py:run`: acquire a fresh saved
SDK fork, compact through original ACP with actual configured system/tools/model,
verify journal/native commit, then one distinct private input and joined shutdown.
It must use the matched new package/runtime, not old4b or original failed inputs.
The existing cold-retained manual/adaptive tests additionally check fresh native
reopen, but their `--no-tools` launch cannot by itself qualify full tool admission.
All application/provider checks remain unrun by this artifact preparation.

## Continued-session queued prompt coverage (2026-10-07)

Parent delivered Core726 and the new native package; the original 147 MB
configured fork passed summary and a fresh answer. The next UX drain failed
STARTED user corroboration before a new prompt. Human text did not differ.
Entry b741a2ea, native input aab39cc6db31e06d87260a283892352c, belongs to
original input acp:268ba30afc1c4b66b53f02f09b9ea2f4. Its recorded digest
54e30598ba000511e6fc6a1369fe7dd99e60f7b0706e58b809dd08967f178b6e
matches the exact original prompt envelope with streamingBehavior=steer.
Ordinary behavior with the same text yields
b48ebdcd2796092c3ef335a019a2cc906e9ecfde9afeffdcfa9edfeb2f48e54a.
Read-only comparison of all 74 original STARTED entries found 69 ordinary
prompts and five steering prompts, with no changed text or unmatched envelope.

Native _claimNativeInput hashes the full prompt envelope, including queue
behavior. InputForwarding/send_prompt and InputDrain legitimately emit steering
prompts; the SDK persists their original digest. The verifier reconstructed only
an ordinary prompt. Prompt now owns exact digest corroboration over its declared
ordinary/steer/followUp modes. The original private_sidecar encoding/hash owner
supplies the envelope; initial prompt binding and private send stage retain
their ordinary default. No request ledger, mode storage, compatibility decoder
or native protocol change was introduced. Exact text/content, unique identity,
owner, live context evidence, ancestry, revision and UNKNOWN/raw-marker refusal
remain required. Standalone steer/follow_up envelopes, images and changed request
configurations are not newly accepted. Durable formats remain unchanged.

Existing Package acquisition covered 324 source, 381 tests and 54 tools modules
with zero parse omissions. Before/current declarations and consumers are in
/home/ts/.cache/agent-scratch/mfc01/continued-owner-consumers.json.
Native SDK patch/declarations were read directly; dynamic external extension
behavior is not proved absent by the Python inventory.

The original private-store continuation check covers all three declared prompt
modes: three passed in 0.31 seconds. All 14 existing uncertainty, identity, text,
digest, content-shape and revision refusal cases passed in the first batch.
Its positive fixture initially wrote the authored journal after capturing the
reserved revision and correctly failed on session_changed. The fixture now
captures revision after writing; only the three affected positives were rerun.
Both logs and authored stores remain in mfc01. No public reservation, compaction,
prompt, restart or replay occurred. Inputs 780 and 778 remain untouched.
Parent must integrate/build the changed Core before isolated saved-session
continuation/summary acceptance. Native package and pin need no rebuild for this
original-envelope correction. The parked field_codec change is excluded.

### Original 74-start verifier invocation

`verify-original-continued-coverage.py` invokes the actual continuation verifier,
not just Prompt.matches_recorded_digest. It acquires shared original wire,
registry and input locks, borrows the real NativeEvidenceRead, and uses the
original journal read transaction and PrivateRawInput rows. The verifier itself
reads/corroborates the coordinator and native context evidence. No writer
CompactionJournal, reservation, fork, SDK, drain or input send is constructed.
Existing lock files must already exist; acquisition is nonblocking.

The current registry has no active UX turn. The original failed admission and
its process-local queue cannot be reconstructed from a diagnostic. This helper
uses ManualSource as an observational value from the actual current owner,
recorded last completed turn and current SessionRevision. It claims current
original historical coverage only, not the failed admission or manual-compaction
authority. No pending keys or queue exclusions are invented: any unsettled input
refuses the stricter observation. It requires exactly 74 native STARTED entries
and preserves the original source-currentness fences. Concurrent changes or
unavailable locks are failures, not a reason to retry or fabricate a result.

Use the reviewed installed interpreter without PYTHONPATH/source overlays:

```
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
 /home/ts/wt/toad-sidebar-context-pointer-20261006/.artifacts/continued-input-delivery-20261007/runtime/bin/python -B \
 /home/ts/wt/comms-goal-ledger-schema-carry-20261002/evidence/inbox-compaction-source-20261006/verify-original-continued-coverage.py \
 /var/tmp/agent-comms-live-20260927-wzjtqhza agent-comms-ux
```

Prepared and source-compiled only. Parent owns its actual invocation alongside
the new installed Core acceptance. Pending inputs remain unmodified.

### Recovery before native admission

Input 780 has an engaged execution and reservation without recorded send epoch
or session identity. Missing admission does not prove no child or no send.
NativeAdmissionEpoch now supplies original launch selections: recorded admission
uses its journal; unrecorded admission requires the exact current stopped release
and uses its saved-session selection, if any. VerifiedOwnerLoss borrows that
actual receipt and owns the shared exit check for both recovery consumers.
Both --session and allocated --session-dir forms must be absent. No durable
format changed. Live-owner/native, publication and current-attempt refusals
remain; terminalization stays UNKNOWN and replay unsafe. Failed-native recovery
still requires its admitted journal and genuine failed-terminal evidence.

Before/after consumers: /home/ts/.cache/agent-scratch/mfc01/recovery-owner-before.json
and recovery-owner-after.json. All 324 production, 381 test and 54 tool modules
parsed/compiled without omissions; old scanner sites are gone. Existing private
recovery checks failed during fixture setup before creating a reservation:
swallowed NativePiUnavailable followed by missing row. Ten setup errors are
preserved in mfc01/recovery-check.log; no recovery assertions entered. No live
operation occurred. Unrelated dirty field_codec work is excluded.

Parent operator call, using corrected installed Core without source overlay,
ONLY after original normal stop produces the exact current dead-owner release,
BEFORE replacement restart. A refusal stops the operation, not automatic retry.

```python
from agent_comms.coordinator import Coordination
from agent_comms.attempt_recovery import RecoveryMonitorCapability

with Coordination('/var/tmp/agent-comms-live-20260927-wzjtqhza/coordination.sqlite3') as store:
    result = RecoveryMonitorCapability.abandon_released_native_attempt(
        store,
        'wirev19c4215bec1f0a8c69c773d5e43ee172d1f4691bcda6338dc82f7d1a1745c8466',
    ).value
    print(result.execution.reason_code, result.is_current, result.can_retry)
```

Expected relation: released_native_unknown, not current, not retryable. This is
not NotSent or delivery success. Neither 780 nor 778 is replayed.

### Independent current Core context inspection

Current instructions/awareness are owned by TurnContext.for_inspection, not by
the selected SDK preview. RuntimeRequest now declares two independent members:

- ContextCoreRuntimeRequest / context_core(thread) returns FieldCodec TurnContext
  from the actual current backend owner, without acquiring a native request.
- ContextCoreSourceRuntimeRequest / context_core_source(thread, owner:
  ThreadIncarnation, segment: int, manifest: SegmentManifest, source: Provenance)
  reacquires current Core context. TurnContext requires the exact incarnation,
  selected segment.manifest(0) and source membership before reading public text.
  Changed wording, provenance, position or incarnation refuses. FileProvenance
  also retains its original byte-digest check. Zero here is an unmeasured
  comparison projection, not a token estimate or budget declaration.

NativeContextData now contains only original SDK counter, identity and measured
segments. contributors, with_current_contributors, contributor_context and
inspection_segments are deleted with all Core consumers. Original context and
context_source retain selected native session and PreviewProvenance checks.
Recorded context/source/annotations and imported source owners are unchanged.
The CLI acquires Core first through its backend socket; native preview or token
inspection failure returns the Core observation plus the actual error. It does
not fabricate native context or token estimates. Successful native inspection
retains its existing measured output. Parent owns the matching Toad consumers.

Complete HEAD/current AST consumer evidence:
/home/ts/.cache/agent-scratch/mfc01/context-owner-consumers.json.
324 production, 381 test and 54 tool modules parsed/compiled without omissions;
removed coupling names have no remaining sites. Nine source checks passed in
the first batch, including native preview currentness/refusals, recorded source
membership and cold contributor decoding. The new real private socket check
initially failed before entry because its existing fixture root argument was
wrong; after correcting that call it passed in 1.14s. It reads Core and an
authenticated source with no saved session and leaves persistent native
backends empty. Logs: mfc01/context-source-check.log and context-socket-check.log.
These source checks ran with the preexisting parked scalar optimization still
present in field_codec.py; that file is neither changed by this work nor included
in this commit. Exact paired installed/UI acceptance remains Parent's next step.
No native SDK/provider, live input, recovery, restart or public mutation occurred.

### Settled recovery metadata table discriminator

RecoveryAudit.kind is the original recovery-condition column. The inherited
TypedTable family tag also defaulted to kind, so FieldCodec correctly refused
encoding the table within RecoverySnapshot after the successful live recovery.
The complete declared table family has three kind-bearing rows: RecoveryAudit,
CohortDeliveryReceipts and GoalHistoryEntry. GoalHistoryEntry already declares
row as its nominal discriminator. Both missing declarations now do likewise;
ProjectedRecovery is only TypedRow and has no family tag collision. All original
kind columns, condition/delivery values, names, constraints, SQL and write/read
algorithms remain unchanged. Other table tags are unchanged. No codec, mapper,
legacy reader, recovery replay or live database mutation is introduced.

AST declarations/consumers: mfc01/recovery-table-kind-consumers.json (zero parse
omissions). Actual private SQLite receipt rows roundtrip with distinct row/kind;
a privately settled snapshot, with its authored audit row read through the
original RecoveryReader, also roundtrips and remains noncurrent/nonretryable.
Both affected checks passed. Initial check selection hit a stale execution
fixture and absent cohort receipts; the first new snapshot check correctly
exposed that ordinary settlement does not itself create recovery audit rows.
Those failed checks remain in recovery-row-check.log and
recovery-row-current-check.log; the final snapshot pass is in
recovery-snapshot-encoding-check.log. No live recovery was repeated. Checks use
the preexisting parked field_codec scalar change, which remains excluded; its
conflicting-field declaration check is unchanged. Parent owns integration and
any read-only original-result encoding after delivery.
# Bound Restart configuration — 2026-10-07

Joined current main normally before this correction. Main already puts editor
field projection on CliCommand and gives RestartCliCommand an empty projection;
TargetAction delegates to that declaration. The remaining execution hole was
CliCommand.edited accepting any non-target-bound CLI field. It now accepts only
the fields the concrete command offers. TargetAction.edited, TargetEdit and
single/batch execute_target all use this owner; no UI command-name decision was
added. Explicit CLI parsing still uses the complete command declaration.

Restart binds only the selected name. Empty target edits leave agent_bin and
agent_args unset, so original OwnerRestartRequest/RetainedOwnerLaunch custody
still supplies the captured launch. No lifecycle, saved-resource, native or
codec change was made. Parked field_codec.py edits remain excluded.

Existing refactor-audit Package.load parsed all 324 source, 381 test and 54 tool
modules with zero omissions; declaration/call searches found the editor family
in cli_commands.py and its existing CLI/channel controls. Dynamic external
callers are not ruled out; they receive the same declaration-owned refusal.

Three focused checks passed in 0.34s: actual private-store Restart catalog and
edit refusal with authentic observed process identity, retained restart handoff
codec preserving complete original launch configuration, and mixed catalog
editor/confirmation behavior. Explicit CLI override parsing is checked in the
catalog control. No worker restart/native request occurred. An initial command
named a nonexistent third test and stopped before test entry; both logs remain.
Raw: /home/ts/.cache/agent-scratch/mfc01/restart-editor/check02.stdout.log and
check02.stderr.log. Installed UI delivery remains Parent's next action.
