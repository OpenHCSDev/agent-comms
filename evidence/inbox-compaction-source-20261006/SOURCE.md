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
