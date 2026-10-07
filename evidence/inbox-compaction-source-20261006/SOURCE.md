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
