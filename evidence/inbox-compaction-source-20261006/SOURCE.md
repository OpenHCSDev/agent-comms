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
file reference. `DrainDiagnostic` owns that optional reference. The new decoder
reads old events; this does not establish old-decoder compatibility.
Availability, stop/propagation and clearing behavior are unchanged.

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
observation deduplication, stale-owner refusal, codec roundtrip and unchanged
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

## Backend delivery and pr159 recovery

This changes the durable activity declaration. I read both actual installed Core
copies under the sidebar-live-candidate deployment: backend `runtime` and frontend
`runtime-send-focus`. Neither installed `DrainDiagnostic` declares
`diagnostic_path`; both installed `FieldCodec` implementations reject unknown
dataclass keys. The new encoder includes the field even when null. The passing
legacy control proves only that the new decoder accepts old rows.

The decoding relation is `Activity.from_wire` -> `FieldCodec` family/dataclass
decoding -> `ActivityLog._latest_events` -> `AgentActivity.activity_of` ->
`ThreadView`, thread/history presentation and notifications.
`ActivityCheckpoint.latest` also contains these Activity declarations. Discarding
an unreadable derived checkpoint does not bypass authoritative log decoding.
Backend workers read other owners' activity through this family. Toad sidebar
coordination access constructs Core Comms/snapshots in its own interpreter, so
its installed Core decoder must also change. CLI readers and retained clients
reading this log are affected. ACP configuration metadata is unchanged.

Delivery requires a truthful new Core wheel/proof and a coordinated backend and
client cutover through the existing OwnerRestartRequest, retained launch handoff
and stopped-batch publisher. ReviewedFrontendCohort cannot admit this as unchanged
backend resources. Toad production can remain unchanged, but its frontend prefix
must contain compatible Core. Retire or upgrade every old reader before a new
writer emits the field. Compare the final joined source and forced resources to
the actual deployment; no old wheel/proof/resource identity is relabeled. Native
bootstrap/package bytes are unchanged by this fix. No build/publication occurred.

After a new diagnostic is written, reinstalling the old Core decoder is not a
safe rollback. Keep compatible decoding even if the writer is rolled back; do
not delete authoritative events or relax unknown-field checks. Original
stopped-batch recovery restores acquired installations/launches, but cannot make
old decoders understand newly written records.

A plain restart is unsafe for pr159. Worker.run creates CommsAgent with
runtime_enabled=True and default auto_wake=True; load_session starts the drain.
replay_unknown_inputs only emits ledger notices, but the drain separately selects
sealed PendingNotification assignments. The current read found 30 triage_pending
assignments, sequences 590..619, and an empty execution pointer. Three historical
triage reservations lack context evidence; their association with this failure
is unproved. Idle status and no summary operations do not settle those inputs or
prove the pending batch cannot be selected again.

The safe next operational action is OwnerLifecycle.stop for pr159, using its
current owner checks and guarded process identity, then keep it stopped during
delivery. This preserves session, dispositions, raw markers and receipts. No stop
was performed here. Other owners need not wait for its unresolved cause.

Before enabling its drain, acquire a fresh stable source through the existing
source owners and run verify_continued_private_session read-only, retaining the
actual exception. Do not reserve/send a summary, replay an original prompt or
settle receipts as diagnosis. This checks source revision, input readiness,
native evidence, coordinator schema/receipts, sidecar corroboration and ancestry;
manual receipt equality is insufficient. Existing CommsAgent(auto_wake=False)
supports a session without automatic inbox inputs; the normal headless worker
has no equivalent launch option. Its ordinary start/restart cannot be called
read-only inspection. Any acquired runtime/read session still needs actual
custody; no runtime purpose is supplied by this source change.

The remaining missing fact is the first refusal from that complete current-source
acquisition. The original failed cut cannot be recovered. A concrete refusal, or
successful coverage plus explicit disposition of original uncertain inputs, must
determine continuation. No coverage waiver, participant injection or automatic
retry is proposed.
