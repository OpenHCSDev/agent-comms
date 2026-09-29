# Wake/pump lifetime closure

Base: main40b66442. Wegener owns this source; parent owns merge/live gate.

The nested wake dispatcher and nullable watcher mode loop are deleted from
InputDrain. WakeScheduleCheck owns validation, turn-lock acquisition, current
owner/goal selection, dispatch and standby refusal settlement. ScheduledTurn
owns autonomous/source/dependency semantics. RuntimeServer owns controller-free
background launch. WireWatch owns notification/poll cadence and final descriptor
release; WireChangeWatch and PollingWireWatch provide the behavior cases.
InputDrain retains the sole private observation revision, serial drain lock and
actual diagnostic/configuration observation. No new cache, schema, native package,
input authority or compatibility API.

Patterns: IMPL-4/5 (watch cases), IDEN-3 (no nullable watch mode), IMPL-8
(no captured wake/observation closures), IMPL-12 (one controller isolation path).
The scheduling check is execution authority, not a forwarding facade.

Caller closure: delete InputDrain.schedule_wake; goal and drain callers invoke
WakeScheduleCheck.schedule. Existing tests using the old entrypoint migrate.
No startup, proof, SelectedExecution or process custody edits. Boyle386 notified
in comment5889382288 before edits.

## Verification

- Source first:26passed/6failed (source-first.log). Four failures lacked the
  explicit native package env; two watcher fixtures lacked the current registered
  owner/session binding. Corrected setup then exposed two old flat worktree
  metadata assertions; migrated them to CoordinationChangedUpdate without
  restoring the deleted envelope.
- Corrected lifetime/project/goal-standby:37passed8.33s (lifetime.log).
- Installed affected callers:103passed/1optional native-stack skip/2failed29.15s
  (installed-callers.log). Selector included more ACP cases than intended; no
  broader repeat. Two pre383 tests expected late RequestError instead of the
  actual early InputHandoffRefused; migrated the exact typed refusal and retained
  all no-backend/no-grant-consumption/durable-unresolved assertions.
- Corrected installed refusal plus guards:8passed1.22s (installed-refusal.log).
- Noneditable installed saved-native history -> ACP prompt -> real native503 goal
  failure -> passive projection -> explicit socketRetry -> one fresh native
  continuation:1passed13.33s (installed-native.log). Loopback provider only.
  Installed site-packages and canonical9213 manifest verified. Production source
  cf0ebb82; subsequent changes only tests/receipt/plan.
- Separate urgent current LIVE fork investigation: installed19e5 controls runtime
  with canonical9213, actual41333152-byte saved OpenHCS source -> canonical fork ->
  immediate owner ACP attachment -> exactly one StartedInput and reply. One local
  HTTP post; fork-to-reply10.93s/test11.94s. Source unchanged, child stopped, no
  input retry or replay. Controlled provider proof, not configuredSol or painted
  UI proof. Parent owns the separate stale GUI manifest mismatch and fresh UI gate.
- Ratchet: InputDrain543->454lines, measured god-class excess43->0; no increased measure;
  foreign absence probes reduced5, no new chains/codec subclasses.

Production:152lines deleted/200added. Net growth is the explicit shared watcher
lifecycle and its concrete notification/poll cases, replacing absent-state
branching and two captured task closures; no parallel runtime authority.

Cleanup: seven owned disposable run directories removed after process-reference
checks; source, wheel/runtime candidate, branch and receipts retained. No live
process changes, original history writes or native package changes. Parent owns
paired painted/live activation; this receipt does not claim PR390 installed live.

## Current-main integration

Merged e8865563 (including386/387/389) normally at17376df3. All six390 production
files are unchanged from installed cf0ebb82. The combined source lifecycle/guard
check passed7tests0.69s; ratchet still reports no increase. Existing installed
native receipt is for cf0ebb82, current LIVE retained-fork receipt for19e5. Parent
will exercise the combined installed/live pair; no repeated unchanged native matrix.
