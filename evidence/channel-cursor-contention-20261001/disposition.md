# Six post-triage cursor failures — read-only disposition

Original root: /var/tmp/agent-comms-live-20260927-wzjtqhza.
Read observation: 2026-10-01T22:04:47Z.

All six original wire 319 / message 6c367634bfc0 assignments are terminal IgnoredAssignment revision 4. Each NativeRuntimeInput is TriageNativeExecution with recorded IGNORE, complete original live context, exact original prompt binding, and one successful native assistant IGNORE terminal. Exact journal/context equality was corroborated with existing typed declarations. Original session and .input-proof bytes stayed unchanged.

At observation all six current execution pointers and registry active turns were absent; none had a matching wire319/native-input ACP InputDisposition. These records do not currently block a selected execution or compaction via an unresolved matching ACP input. This does not prove unrelated earlier attempts are settled, or that the UI has refreshed its old error.

## Existing declaration owners and safe disposition

- TriageNativeSend.commit (private_send_stage.py:258-273) records NativeRuntimeInput context/verdict and IgnoreSelectedTriage.settle in the same coordinator transaction.
- IgnoreSelectedTriage.settle (selected_triage.py:51-69) commits IgnoredAssignment before its continue_turn calls CoordinatedTurn.ignored/cursor_status_for. IgnoredAssignment is terminal, has no successor, and declares notification Checked — no response (assignment_states.py:226-244).
- NativeInputExecution.require_attempt refuses triage: there is no FULL execution/attempt for any of these six. RecoveryMonitorCapability.recover_native_failure requires a current attempted execution, failed native terminal and verified owner release, so is inapplicable here. Do not manufacture an attempt or use UNKNOWN abandonment to relabel a successful IGNORE.
- HistoricalNativeInput / NativeContextJournal / PromptBinding own read-only corroboration. SourceCoverage reads the existing settled assignment/proof relation; no receipt, provider terminal or input acceptance is reconstructed.
- NativeSourceCursor is auxiliary. CursorOwner.admits (cursor_owner.py:89-122) refuses seeding a later live admission from an old recorded input. After owner restart do not pass an old input ID to force cursor advancement or weaken the admission fence. Existing historical source coverage is sufficient to derive handling; lack of a current cursor does not reverse the terminal assignment.
- SelectedParticipant.lease finally calls existing agents.finish_turn. CurrentExecutions.require_idle accepts the empty pointers observed here. No recovery mutation is required for these six.

## Causal failure

The preserved diagnostics all show cursor _response_boundary nonblocking wire/bus flock raises BlockingIOError after triage settlement. It propagates through PrivateEvidenceRead.open yield and is incorrectly wrapped as NativePiUnavailable; SelectedRequest.native_failures emits the misleading backend/provider failure alert. Arendt498 owns correcting that existing evidence-reader consumer boundary. No new recovery mechanism is needed.

## Boundaries

No public writes, owner starts/stops, schema bootstrap, recovery/reset/retry, ACP/native RPC or provider calls. SQLite connections were mode=ro and query_only. Avoided NativeContextProof.read_evidence because its current NativeContextJournal.open_evidence deliberately opens mode=rw for possible hot-journal rollback; used the same declared NativeContextJournal.for_input/corroborate under mode=ro instead. Report files are evidence in owned scratch, not semantic state. Registry/coordination/InputDocument were separate read observations; fresh locked owner checks remain required before any future operator write.

## Original inputs

| Thread | Native input | Native terminal |
|---|---|---|
| openhcs-audit-merged-boundaries | 8f71f8d42eb047fa9f580cb5ebc92f94 | e47a4c9f |
| openhcs-pr159-viewer-bind-owner | a2eb1f0b63dc1c17cc0e8a0ef5bd5281 | 7b424732 |
| openhcs-audit-open-prs | 3e7d11443ce31ca737a529b54be86b35 | ecb3c409 |
| openhcs-audit-merged-runtime | b7e4bd81cc1979b4281e2a1a221cb1a9 | 4816267e |
| openhcs-architecture-memory | 17120a171c1852cebf305d9f447c39bb | cc52beec |
| openhcs-audit-merged-models | 2364d8396a32d69b0f9fe0b5ed3badd3 | 8455ec25 |
