# Backend response lifecycle — PR294 ready for parent review

Depends on PR288/d155c6ff. Implementation85dc9eb0, actual-path tests414ebfbb.
Parent owns merge/install. No native package change;5fde remains canonical.

## Original plan and owner closure

Read S1/S2/S7, round2 rules, current audit and the actual backend/event/command/
channel/request/phase/input/process owners. This closes original S2 G4 response
meaning/correlation and the associated S7 backend residue, not all remaining S7.

- PiEvent.consume owns shared progress around per-event behavior and phases.
- SessionSnapshot/MutatesSession own identity implications through command MRO;
  TurnSession owns the actual invalidation reaction and child shutdown.
- Prompt/InputForwarding own prompt acknowledgement; interruption behavior is
  on InterruptSteering and the two existing interrupt events.
- StatsRequest holds the canonical pending response futures. Completion, failure
  and busy state derive from those responses. Deleted its duplicate response IDs,
  ID set, mutable completion flags and backend snapshot-response switch.
- PiRpcChannel.track exposes the same PendingRequests future used by encode.
  There is one correlator, not another broker, registry, ID map or facade.
- Deleted backend guard_identity/observe_progress/settle_or_continue, their calls
  and scratch fields. No aliases/re-exports/dual execution paths introduced.

ChildProcess supervision, retained native-session proof and source stores remain
with their existing owners. Dalton baseline fixtures, Boyle stores and Toad
surfaces were not edited. No reset, replay, paid call or live installation.

## Local and actual installed evidence

- Backend external recorded-protocol/real-child-pipe suite:192pass28.57s.
- Shared Pi declaration/payload/settlement suite:68pass1.83s.
- New command-capability extension and deletion guards:3pass0.09s.
- Noneditable installed wheel:12 actual pinned-native/localHTTP cases pass43.47s.
  Includes ordinary queued input with exact native start IDs, one settlement,
  retained child reuse, strict saved-session validated reopen,2,097,409-byte
  response, ordinary cancellation and EOF, and all9 PR288 tracked-native cases.
- Same installed wheel: actual selected summary -> native commit -> exactly one
  original admission passes6.22s. Compaction journal/proof path not bypassed.
- Lint passes for all7 source files and both new test files; diff check passes.

The first actual harness omitted production streamingBehavior=steer and timed
out; cancel/EOF passed. That FAILED receipt remains native-source.log. A second
variant proved queue/reuse/large output but could not admit another input after
its deliberately oversized response; native-corrected.log stays FAILED. The
final fixture validates reopen before producing oversized output, preserving
all assertions and the native context budget. It does not claim oversized
history is eligible for another prompt. The corrected source case passed9.07s;
then all three cases passed in the installed12-case batch.

NRA global source context was scanned before and after. Raw22 findings unchanged;
CLI omitted detector-coverage/scan_status fields. No zero-debt or equivalence
proof claim. See audit-before.json/audit-after.json for scope and R1 output.

## Accounting and boundaries

Delta versus d155c6ff: production192added/177deleted (net+15); backend1539->1409.
Added lines express explicit declared behavior and futures-derived state while
removing independent correlation state. Tests293added/0deleted: actual ordinary
native lifecycle coverage was missing beside recorded-protocol tests; new-case
and deletion guards protect extension/deletion. No obsolete baseline fixture
porting was performed. Existing29 baseline failures stay with Dalton.

Wheel: .artifacts/backend-response/wheel/agent_comms-0.1.0-py3-none-any.whl
Installed: .artifacts/backend-response/installed
Owned derivative histories/cache removed after processes exited; logs/wheel,
source/branch and canonical native package preserved. Cleanup receipt adjacent.
