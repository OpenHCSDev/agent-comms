# Issue 714: pending alert versus idle activity

Owner: agent-comms-ux. Source checkpoint: main 058ee830f.
This is an investigation checkpoint, not a working fix or live acceptance.

## Recorded read-only evidence

Wire 771 / 57ff573dd873 targets comms428. Its WakeAssignment is full_pending
and has no execution_claims linkage. Registry active_turn is absent.

CurrentExecutions retains wirev162960e0b834144ac3c80087ca6e918abb489ff02cf88c6effd4c408a00377bc4,
originating from wire 234 / 789543e2729c. The attempt remains model_running,
owner_generation=2, backend_done=false, process_dead=false. No progress was
recorded after September 29. The current OS worker is a later process.

Original input b6c82048ea29f032122f21ad7d2be62a records dispatched admission
938 but no certified session identity, context receipt or verdict. No original
release receipt was found for comms428. A nearby aborted assistant entry in the
saved session is not certified as this input's terminal; it cannot justify
retiring this execution.

## Existing owners and consumers

- CurrentExecutions / ActiveExecutionSlot own unresolved execution exclusion.
  Their refusal preserves pending claims rather than starting parallel turns.
- NotificationAssignment.select joins CurrentExecutions into AssignmentActivity
  through an observing connection with bounded lock acquisition.
- AssignmentActivity.blocks compares acquired custody with the assignment's
  own execution. LiveRecipientActivity renders 'Blocked by earlier turn' when
  no registry turn is executing.
- ThreadView captures registry/activity/runtime/goals but not that coordinator
  custody observation. PeerState.from_view serializes activity directly.
- HistoryViews.list_threads also emits activity directly. Thread presentation
  acquires message notifications separately. Thus activity-idle is not proof
  of dispatch-readiness, but current peer context offers no such distinction.
- AttemptRecovery.recover_native_failure requires observed original release,
  bound certified session identity, and an unambiguous failed terminal. This
  incident does not currently meet those prerequisites.

## Existing drain readiness mechanism (follow-up)

InputDrain.watch already records owner-fenced UnavailableDrainDiagnostic on a
CoordinationError; DrainReadiness feeds roster presentation and notifications.
SelectedParticipant.prepare calls participant.pointer.require_idle when pending
claims exist. The latest recorded comms428 activity has no diagnostic at all.
Therefore adding a new roster acquisition is premature: first establish whether
the attached worker actually observes this pending claim, catches the custody
refusal, and records its existing diagnostic. PeerState does bypass interpreted
readiness, but the missing diagnostic is a second fact requiring investigation.

## Next coherent change

Acquire roster delivery-readiness through the existing read-only projection
owner once per roster, with canonical participant identities. Expose it through
ThreadView and its peer/list/sidebar consumers, preserving actual activity as a
separate fact. No copied execution state, per-peer DB reads, or optimistic idle
fallback on an unavailable observation. A message-scoped blocked notification
must not be relabeled as a universal peer state without tracing its semantics.

Recovery remains separate: preserve the original UNKNOWN evidence, determine
whether original custody evidence survives, and never synthesize terminal proof
from process replacement, lease expiry or registry idle. Do not replay wire 771
or the original input.

## Acceptance still outstanding

Source census across these declarations and every consumer; coherent migration;
focused checks; isolated busy-to-idle/uncertain admission journey; actual
installed peer/sidebar display. No implementation or tests claimed here.
