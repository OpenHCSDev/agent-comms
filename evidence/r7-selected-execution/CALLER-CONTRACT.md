# Current R7 caller contract

`SelectedExecution(root=..., wire_root_id=..., owner_name=..., native_package=...,
...).run()` replaces `run_one_sealed_claim(...)`. One instance is permanently
consumed by the first run, including preflight failure/cancellation. Existing
keywords retain their meaning. No asynchronous compatibility wrapper remains.

`WakeAssignment`, `AssignmentState` and concrete `*Assignment` cases replace the
attention `WakeClaim` / `*Claim` names. `assignment_states.py` replaces
`claim_states.py`. Resource claim APIs/strings keep their meaning.

- MutationStore.assignment/accept_assignment; create_execution assignment_ids.
- sealed_cohort_assignments, AcceptedCohort.assignments/assignment_count.
- WakeAssignment/PromptBinding/HistoricalNativeInput/CurrentNativeCursor and
  CoordinatedTurn.assignment_id; WakeAdmission.wake_assignment_id.
- RecoverySnapshot.assignments; FieldCodec.project(snapshot, "snapshot").
- Recovery projection records use FieldCodec.encode directly; ProjectionRecord
  and its to_primitive forwarding are deleted.
- ParticipantSnapshot.participant_generation is the actual field; generation
  and its duplicate accessor no longer coexist.
- Registration.lease_live_turn_with_admission / lease_live_turn_with_generation /
  lease_local_turn and RegistryDocument.lease_turn are the current turn API.
- Internal owner/admission *_epoch parameters/fields are *_generation, including
  owner compaction attestation/source and selected-summary enrollment callers.

Existing serialized claim_id/claims/wake_claim_id/generation/owner_epoch/native
admission_epoch spellings remain boundary spellings declared once via FieldCodec
metadata or consumed at their existing SQL/native boundary. No live migration,
second decoder, alias, new ID allocator, generation change or replay is needed.

## Pascal R6 overlap

Only `thread_management.py` overlaps the observed R6 production write set: three
`person.generation` -> `person.participant_generation` accesses in rename's
coordinator journal/CAS/rollback. Method-local patch in thread-management.patch.
No transcript/native-entry/replay/PiPayload file is changed.

Parent owns any paired Toad call sites and serial installed rollout. ACP external
metadata remains unchanged (including its ownerEpoch/cursor serialized fields).

## Current paired Toad caller found

Parent tree `toad-export-caller-migration-20260928`:
`tests/channel_history_reader_pilot.py:58` calls
`comms.registry.claim_local_turn("peer", "new-turn")`; migrate this to
`lease_local_turn`. No production core API import/call matched this removed
surface. Toad's own cursor-scope `owner_epoch` and JSON owner_admission_epoch
remain its existing protocol; the core metadata still emits those spellings.
