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
file reference. `DrainDiagnostic` owns that optional reference; old events still
decode without it. Availability, stop/propagation and clearing behavior are unchanged.

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
