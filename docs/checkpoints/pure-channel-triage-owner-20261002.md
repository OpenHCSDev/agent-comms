# Pure channel TRIAGE to FULL: existing assignment owner

## Source checkpoint

Original public seq326 has twelve recipient claims, ten proved native TRIAGE
inputs (nine FULL and one IGNORE). Nine FULL originals remain Deferred at revision
2 without executions. Two claims remain pending. The bounded native reservation
advanced each original TRIAGE_PENDING claim from revision 1 to Deferred revision 2.
The FULL verdict correctly leaves engagement to ExecutionStore, but the selected
wire source copied revision 1. ExecutionStore.create then rejected equality with
the current revision 2 and rolled back its execution transaction.

The existing selected_source_batch.SelectedSource now stores the original claim
ID, its AssignmentStore owner and its sealed CommittedDelivery. Its assignment
property derives current lifecycle from that store and requires the original
sealed source relation. SelectedSourceBatch captures membership once and reads
its assignments in one existing coordinator read scope. It does not admit late
originals into an already captured batch. SelectedParticipant constructs that
relation directly. ExecutionStore requires its own assignment-store capability,
reads current claims inside its existing write transaction, and engages with
the current revision CAS. The obsolete comparison with a copied lifecycle row
is deleted. No refresh after triage, new class, registry, queue or state mirror.

TriageNativeSend.commit returns the authoritative settled rows from the same
transaction which records the original native context/verdict. The existing
SelectedTriageOutcome/SelectedTriage members now OWN async continuation: FULL
creates through ExecutionStore.create_after_triage with the exact settled-row
witness, IGNORE consumes reply wait and returns its terminal result, and rejection
returns its existing failed result. Direct FULL creates from the transaction-owned
source directly. One ExecutionStore._create algorithm owns actual mutation for
both entry paths. The proved triage entry refuses any intervening row change.
SelectedAttempt receives the actual created RecoverySnapshot and starts the
attempt with its original revision/pointer fences. No new optional/None state,
strategy type, member wrapper, compatibility path or cached lifecycle.

SelectedParticipant.require_current reuses the existing registry/participant
owners before either execution creation and attempt start. SelectedSourceBatch
owns the existing deterministic execution identity derived from immutable ordered
assignment IDs; the runner's copied construction is deleted. FULL continuation
is outside the already-proved TRIAGE request's failure context: the FULL native
request retains its own failure/UNKNOWN owner. Action selection occurs on the
continued session, preserving the existing FirstSelectedSession action behavior.

The other selected_source.SelectedSource is the existing native selected-summary
DeclaredFamily (filesystem/session revision and coverage). It does not own wire
claim lifecycle. It remains a distinct required semantic relation; this change
does not create a second family or copy its authority.

## Caller closure

- SelectedParticipant.select/source: original sealed receipt and immutable
  recipient/delivery membership; AssignmentStore owns current claim rows.
- SelectedPrompt.triage/full: original delivery plus selected/current stage rows.
- SelectedConsideration and TriageNativeSend: capture exact stage rows for native
  claim/phase fences; return the proof transaction's settlement witness. Original
  reservation, context and verdict unchanged.
- SelectedAttempt.engage and ExecutionStore.create: same original IDs, current
  mutable lifecycle derived inside the execution transaction, strict revision CAS.
- FullNativeSend/AttemptStore/RecoveryReader: exact engaged snapshot and evolving
  attempt fence remain owned by the existing attempt family.
- SelectedSourceBatch route instruction/replies: sealed delivery owns original
  route; no route or audience copy. ResponseObligation/publication/history/cursor
  continue to join original IDs and native proof membership.
- CoordinatedTurn and reply-wait settlement: captured IDs/sequences unchanged.
- All seven direct constructor callers in three existing fixture modules now
  pass the same AssignmentStore relation; no compatibility constructor remains.

InputDrain catches the IdentityConflict as a CoordinationError and records an
UnavailableDrainDiagnostic. DeferredAssignment then presents Paused. This is
not a native provider failure or a missing FULL verdict.

## Preservation and final acceptance

Original seq326 proof references are recorded in
evidence/pure-channel-triage-owner-20261002/original-seq326.json without bodies or
credentials. No original row, input, disposition or public store is changed.
Deferred originals are excluded from ordinary pending admission; this change
does not add a replay/recovery permission. Parent owns their explicit disposition.

Order: existing-owner and complete caller reasoning, coherent implementation,
then batched validation and the actual installed many-owner pure-channel journey.
That journey must take bounded TRIAGE then FULL with no human DM or mention
forcing FULL. Current checkpoint is source only, not installed/live acceptance.
