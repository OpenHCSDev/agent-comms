# S3/S7 MutationStore ownership closure

Assignment: continuation after PR292, in independent persistent worktree
`~/wt/comms-mutation-owners-20260928`. Parent/Dalton own integration, candidate
acceptance and live installation. No candidate or live checkout edited.

S3 expressly includes coordination_store.py and assigns genuine cross-lifecycle
invariants to named owners. S7 includes residual MutationStore mechanics, requires
components owning state rather than shared-self mixins, and caps modules/methods
at1,000/100 lines. C0's current override requires complete caller migration and
aggregate deletion; round2 forbids compatibility facades/reexports/second registries.

## Actual ownership

- Deleted coordination_store.py and MutationStore. Coordination is a composition
  and explicit-bootstrap root with no forwarded domain methods or attribute proxy.
- CoordinationSession inherits the existing SQLite connection owner and owns the
  clock, non-nesting write transaction, exclusive irreversible admission, read
  snapshot reuse and rollback/commit lifetime. There is one connection per root.
- ParticipantStore owns participants/aliases/generation aggregates. AssignmentStore
  owns acceptance and preengagement. ExecutionStore owns creation/unstarted changes.
- AttemptStore owns fenced attempt progression, leases, replay and atomic settlement.
  AttemptStart owns immutable admission identity, replay matching and the explicit
  cross-lifecycle eligibility rule. Activation and retry reengagement stay inside
  one transaction: no partial publication or new independent database.
- RecoveryReader composes typed rows within one committed read snapshot. Recovery
  monitor grants remain separate from owner fences; duplicate replay SQL is deleted
  in favor of AttemptStore's canonical monotonic recorder.
- DurableTurn receives the actual AttemptStore capability, not the whole coordinator.
- Errors, token preparation and reason bounds moved to their existing declaration
  owners. Applied/AlreadyApplied have a common result owner. No schema change/reset,
  converter, old reader or second roster. Existing TypedTable membership remains
  canonical for explicit private bootstrap.

## Dependency analysis

Session depends only on the connection owner. Participant/assignment stores depend
on session and typed rows. Reader depends on those two stores. Execution/attempt
stores depend on reader/session/participants; admission request depends only on row
and participant snapshot values. Root composes them in that order. The recovery
monitor imports the root; the root never imports the monitor, preserving separate
trusted-construction authority. Native/private schema imports remain inside explicit
bootstrap, not inside reader entry points.

## Executed checkpoint (before current-main integration)

- focused-installed.log: 78 passed,19.85s; real SQLite transaction/fence/CAS/replay/
  recovery/response/bootstrap tests and permanent ownership guards.
- native-installed.log: 2 passed,30.44s; installed pinned native Pi, loopback-only
  model, production detached peer wake, durable response, restart and second input.
  Both fresh protocol and base-only bootstrap scenarios exercised. No paid provider.
- Red receipts preserve the removed test-module import and missed reader/writer
  connection paths during caller migration. Assertions were retained; actual
  concurrent SQLite snapshot tests pass after their callers target the session.
- NRA before/after scans: complete src/agent_comms dependency context, raw/full JSON,
  one parser and analysis worker,150s internal/165s wall bound. Completed31.014s /
  14.899s. Both retain one repeated-builder lead; no claim of a globally clean scan.
  This CLI emits no detector omission inventory. Ownership/caller edits are manual,
  not claimed NRA-proven rewrites. Actual behavior evidence is separate above.

Current-main integration and final local receipts follow; no CI wait.
