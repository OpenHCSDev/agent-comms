# Coordination state receiving checkpoint

Owner: Mendel. PR457. Baseline: `6feb634ba184d94d763028406152fff07027cccc`.

## Original C0/C3 source census

The original five Core C3 families are implemented at this head, rather than
being exempted as external taxonomies:

| Original site | Current owner and consumer |
| --- | --- |
| `goals.GoalMentionBinding` resolution | Declared binding members own projection; `ResolvedMentionBinding` owns its original peer incarnation. |
| Both `relationships.edit` action branches | `RelationshipEdit` command declarations own mutation and retention; both callers construct/invoke that command. |
| `acp_failure._error_detail` raw shapes | `ExternalFailureData` / `StructuredErrorValue` use the existing MRO dispatch boundary for external JSON values. No error text grants retry. |
| `maintenance_barrier.current_unlocked` lifecycle | `MaintenanceState.require_marker` joins the original marker, phase and root; the reader decodes through `FieldCodec`. |
| `thread_status.allows_control` names | Owner control declarations and `ThreadStatus.allows_owner_control` own availability. The old string capability method is absent. |

The original C1 selected-summary consumer invokes decoded data's `response`;
C2 restart state is a declared family and its existing watcher remains sole
execution owner. Parent retains the original ledger and current installed
release receipts; this census does not substitute for those receipts or close
the other owners' fresh S14 leads.

## Working state closure

- `WakeAssignment` owns initial admission and frozen acceptance equality;
  lifecycle progress is the only normalization. The nine-name immutable roster
  and eight-condition acceptance reconstruction are deleted from the store.
- `AssignmentState` constructs and validates its preengagement successor.
- `ReplayAssessments` owns its original safe-replay facts; `ExecutionRecord`
  joins these with its declared retry budget and original wire obligation.
- `DeferredExecution.can_retry` consumes terminal attempt finality.
  `TerminalAttempt` owns done/death/no-lease shape validation. The snapshot
  deletes repeated raw proof checks and the parallel retry predicate.
- `WakeAssignment.require_selected_source` compares the original committed
  source and complete frozen recipient/decision values. Both selected admission
  and framing call it; the second selected-recipient comparison is deleted.
- `AssignmentState.wake_frame` owns pending-triage versus engaged response
  behavior. Framing and `SelectedPrompt` no longer pass a string phase.
- `ClaimOwner` validates its resource, historical owner and source facts;
  `ClaimProjection` validates membership and frontier only.
- Goal source validation uses `GoalRevision`, `TextDigest`, `ThreadIncarnation`;
  the original flat wire fields remain unchanged. Missing-history incarnation
  sentinel `-1` is retained. Inputs strictly decode original text/admission
  provenance and preserve the public boundary error with its decoder cause.

No tables, persisted fields, registry format, native module, source index or
historical reader has been added or replaced. Goal/history/UNKNOWN evidence,
default runtime and all public owners are untouched.

## Verification so far

- Initial affected source selection: 36 passed, one old fake native-event test
  failed. Its exact untouched 6feb source baseline fails identically in 0.14s:
  `test_durable_turn_records_native_phases_before_completion` expects
  `tool_running` from a handcrafted `tool_execution_start` event, while the
  canonical phase remains `model_running`. Arendt owns that fixture migration;
  no production workaround or weakened assertion is introduced here.
- After source, admission and boundary changes: 38 affected checks passed in
  1.57s, including idempotency, preengagement rollback, generation fencing,
  monotonic no-replay safety, forged/unengaged wake refusal, goal mentions and
  historical incarnations.
- Resource assertion warned: home17.4GiB / RAM16.6GiB / swap9.4GiB. One bounded
  serial private native gate is proportionate; no fleet/build matrix.

## Still open before Ready

- Normal integration of Arendt's published NativeInputIdText owner, then delete
  `InputAttempt`'s competing native-ID regex. Arendt owns the native proof builder
  and redacted recovery reader consumer of the published retry contract.
- Sch owns stable-lookup scalar/source projection integration. GoalWait still
  joins original replies via today's mutable sender registry. Trace original
  certified sender/recipient refs for all wait/input-review consumers; do not
  add another query index, seen list or attribution store.
- Run the existing installed selected native local-provider journey through
  triage, full tools, one original response, source proof and lease retirement.
  Source passes above are not installed/native readiness.

Owned scratch: `/home/ts/.cache/agent-scratch/comms-coordination-state-s14-20260930`.
The disposable baseline source extraction and future wheels/stage are owned here;
original source, receipts, private proof journals and UNKNOWN inputs are protected.
