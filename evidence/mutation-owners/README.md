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

## Final current-main checkpoint

Source head: cc8140e2. Actual merge base: 03b6f9f4 (combined288/294/292/290
integration e2191bbe plus already-merged cursor recovery299). Retained current-main
tracked-turn behavior and its repaired acceptance fixtures. No live root, launcher,
route, native provider implementation or Dalton candidate edits.

- install-main.log: noneditable wheel installed in this worktree's own virtualenv.
- focused-main.log: 78 passed,1 failed in16.87s. The failure was our architecture
  guard accidentally renamed to reject the new Coordination declaration during
  caller migration. Restored its intended assertion against retired MutationStore.
- affected-callers.log: 117 passed,2 failed in65.79s. One existing released-owner
  fixture lacked its ProcessIdentity import; one atomicity injection still patched
  deleted _settle on the root. Import restored; injection now targets the actual
  AttemptStore.settle_checked implementation. Assertions and rollback checks stay.
- caller-closure.log: both ownership guards and both failing caller cases passed,
  4 passed in1.77s. The initial red receipts above remain; no claim that those
  complete initial invocations were green.
- native-main.log: 2 passed in24.57s after current-main integration, using the
  installed wheel and actual pinned Pi entrypoint/detached production peer.
  Both fresh protocol and base-only private bootstrap completed send/reply,
  owner stop/start and another send/reply. Only the model endpoint is local;
  SQLite, registry, native CLI, owner launch, wake, journal and bus are actual.
- ratchet.json: source cc8140e2 against actual main merge base03b6f9f4;
  zero positive deltas, including per-class size. New owners have no prior class
  baseline; the module/method guard separately enforces their1,000/100 limits.
- Current executable source/test/evidence callers migrated. Deleted1711-line
  coordination_store.py and the247-line obsolete historical-format replay script;
  removed duplicate recovery replay SQL and root domain forwarding entirely.
  Production diff is+2204/-1951 (net+253), reflecting separate owned dependencies
  and admission contract rather than pretending the refactor is a net line cut.
- The NRA MonitorEvidence lead was inspected: the two constructions are distinct
  observed recovery outcomes (explicit abandonment vs failed terminal), retaining
  their reason and separate evidence checks. No automatic rewrite/proof claimed.

No diagnosed blocker remains for this MutationStore slice. Other S7 modules remain
outside this ownership. Parent owns merge and live acceptance/install; CI deferred.

## Reproduction

From this branch, create an owned virtualenv and install the wheel plus dev deps.
The focused-main invocation selected test_coordination_store,
 test_coordination_response, test_private_runtime_bootstrap,
 test_coordination_cohort, test_coordination_nominal and guards/test_mutation_ownership.
The affected-callers invocation selected test_coordinated_runtime,
 test_native_send_admission, test_native_failure_recovery and
 test_native_unknown_recovery. Use `pytest -o addopts='' --basetemp=<owned-root> -q`.
The native invocation selected test_private_runtime_bootstrap_native with
`AC_NATIVE_COPIED_PACKAGE=/home/ts/.local/share/agent-comms/native-current-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent`.
No external provider calls or live message replay are required.

Owned test roots, scratch/NRA cache and virtualenv removed after all workers exited;
red/green receipts and the complete source branch preserved.


## PR300 caller closure

Integrated main through e1115526 (including300) without conflicts. Migrated both
new S3 fixture snapshot calls to Coordination.snapshots.get. No aliases or old
root APIs restored. Installed a fresh wheel of the integrated branch.
`main300-family-sqlite.log` records the new S3 declaration-family legality suite
plus existing coordination/store/response/nominal and ownership guards.
