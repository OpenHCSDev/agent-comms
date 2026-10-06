# Actual3931 admission, handoff and unchanged-source recovery

Source evidence: actual `3931f16fe` owner_restart declares RetiredOwnerLaunch
(owner, admission, launch) and StoppedOwnerBatch.wire: StoreLock. New689 declares
(selection, launch) and wire_descriptor:int. Registry/Thread/goal/history stored
declarations match the reviewed current integration, but the acquired physical
phase and serialized handoff do not. Equal stored schema is not phase equality.

Passing unchanged3931 as original_python to the new cross-format
restart_original_thread_format -> ThreadRetirementCutover is invalid. The tool
requires wire_descriptor; launch_thread_retirement and restore_stopped_owners
strict-decode the new handoff. The old kernel does not supply those interfaces.
No format detection, old-owner alias, injected phase, raw mapping, or source
module overlay is a solution. Source-interpreter and unchanged-byte checks stay.

Actual3931 needs no GoalReportMemberRetirement: its registry format is already
current. The existing same-format StoppedOwnerInstallation.complete owns
installation then stopped.launch within the acquired original phase. That path
does not need a cross-version serialized ThreadRetirementCutover handoff. Use it
for a same-format operation only with a coherent original method/runtime owner.
Its original handoff.restore can likewise recover in its authentic original
interpreter after the operation certifies unchanged source bytes.

The outstanding transport consumer is PublishRetainedSummary.recover ->
cutover_child.restore_stopped_batch. It currently serializes a new handoff to the
captured source interpreter and assumes wire_descriptor there. A future exact
3931-source selection cannot use that new transfer child. The declaration owner
must either retain original in-process same-format phase/restore custody or use
explicitly versioned, matched admission/launch/recovery producers. New689 source
is not installed3931, and amended720 P is not a 3931 phase amendment. No process
or runtime version may be relabeled to make recovery pass.

Next source closure stays with Einstein's OwnerCutover/StoppedOwnerInstallation,
RetainedOwnerLaunch and existing publisher recovery family: bind the actual
original method owner and decide in-process completion before any fence; require
that same source declaration at recovery. No second stop/start authority, extra
build/holder gate or public operation is requested. This checkpoint records the
real unresolved version relationship; it does not claim runtime recovery or
silently modify a publisher while another integration owner is active.
