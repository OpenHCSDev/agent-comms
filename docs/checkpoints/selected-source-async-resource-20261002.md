# Selected pending source asynchronous resource closure

## Ownership and implementation

Arendt #509 owns `Coordination.run_async(path, operation)` and cancellation retirement. #512 extends the existing `SelectedParticipant.sources/select` behavior and its ordinary `SelectedExecution.run` consumer. The helper at `0e6034c82256a0efe602593de44328c0ead77cc9` is normally integrated; this PR is stacked on its owning branch. No worker connection, lease or mutable claim resource crosses the shared worker boundary.

The worker captures every ordered original `MessageReference` through ONE `CertifiedSourceRead.references` operation. Original pointer/byte capture completes under certified custody; canonical decoding completes outside that custody. Its own `Coordination` read validates the existing cohort schema, `_receipt_matches`, original assignment membership, `WakeAssignment.require_selected_source` and `ParticipantOwner.require`. Only detached `CommittedDelivery` values return. Existing `SelectedSource` instances bind to the caller's ORIGINAL live `AssignmentStore` after the worker has closed.

`select` is an async context manager. The existing `RegistryOwner` lease CAS encloses the await, a final `require_current` follows acquisition, and lease retirement remains in the original `finally`. Shared `join_retirement` joins cancellation through callback connection closure before that finally can release the lease. Source snapshots do not grant native admission; existing native stage admission still checks its current original owner/source/floor fences.

## Complete caller and declaration census

Search: `rg -n 'SelectedParticipant\.(select|sources)' src tests tools`. One current production caller exists: `coordinated_runtime.py`, now `async with`. One current source sanity consumer exists: `test_selected_source_batch.py`, migrated to the same async contract. `sources` has one caller, awaited in `select`.

Three textual synchronous `select` sites remain deliberately in two historical producer tools: `native_schema_carry_controls.seed` is invoked with the explicitly supplied installed ORIGINAL `source_python`; `seed_thread_retirement_fixture.seed_historical_failure` is invoked by `thread_retirement_installed_pilot` with `AC_PHASE_ORIGINAL_PYTHON`. Those generate original schema4/legacy thread fixtures for one-time external carry, not current runtime consumers. Changing their original producer API would destroy provenance. No synchronous compatibility API exists in current production.

Existing owners searched/read: `SelectedParticipant`, `SelectedSource/SelectedSourceBatch`, `Coordination/CoordinationSession/AssignmentStore`, `CertifiedSourceRead.references/capture_deliveries`, `_receipt_matches`, `WakeAssignment.require_selected_source`, `ParticipantOwner`, `RegistryOwner`, `join_retirement`, `SelectedExecution`. Resource ownership follows existing IMPL-13/IDEN-5 boundaries; no copied registry, executor, lifecycle or source authority was introduced.

## Delivery and remaining acceptance

Production change: two files, 45 added / 31 deleted lines. Existing test caller: 4 added / 2 deleted. Coherent source checkpoint is published before final bounded sanity/actual installed affected acceptance. #509 integration owns the common resource and acceptance collector; its next grant determines collector migration. Prior #503/#507 configured 25.911s native gate remains evidence of that earlier source only, not acceptance of this new async change. No provider/native replay or new run has occurred for #512.
