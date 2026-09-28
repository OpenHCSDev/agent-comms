# S7 InputDrain and PR95 queued-during-summary fix

PR: https://github.com/OpenHCSDev/agent-comms/pull/154
Branch: codex/refactor-s7-input-drain-20260928
Owned persistent tree: /home/ts/wt/comms-refactor-s7-input-drain-20260928
Production candidate: 1dea4f7f7d0652561465abe2cea987e2378abfc8
Base includes PR149, PR150, PR151 and PR152 (9988f6b).

Parent actual installed/configured-provider queue acceptance PASSED. The fresh
session linked and committed its selected summary; original and queued native
input IDs started exactly once in order after compaction. The original reply
was exact; queued reply retained ORCHID-7301, Thursday14:30, cobalt and cedar.
Parent reports no UNKNOWN/active reservations or emitted errors. I independently
read the parent's receipt and verified verified_success=true, distinct STARTED
input IDs and linked/committed records. Parent is merging/deploying PR154.

Receipt: `/home/ts/wt/comms-refactor-integration-20260928/evidence/input-drain/actual-provider-queue-result.json`.
No production changes after1dea4f7;8ec2c01 adds evidence and moves two
fixture interception hooks to their new component owner. Parent retains serial
deployment; this worker did not run a provider or mutate any live root.

## Implemented ownership

- `input_drain.py`: owns original/followup durable admission, backend inboxes,
  queued/restored exact IDs and receipts, native-start/refusal effects, delivery
  cursors, passive awareness, wake queues/tickets/tasks, drain locks/tasks and
  turn-input teardown. 917 lines. No shared-self fallback or second store.
- `input_effects.py`: explicit ABC for existing turn/private-runtime effects;
  turn locks/tasks and backend launch settings remain with their existing owner.
- `acp.py`: internal queue consumers migrated, queue behavior removed, narrow
  compatibility methods/properties preserved for runtime/compaction consumers.
  Remaining turn runner/orchestration is deliberately still ACP-owned (2934 lines).
- `input_disposition.py`: existing ledger owns the original/unsettled/future
  queue source decision. `FutureInputQueue` is an ABC over live owner receipts;
  the view derives from the existing queue, not a second registry or durable policy.
- `owner_compaction_adaptive.py`: only passes that existing queue owner to the
  bridge. Preserve Pascal's independent three generation-consumer renames.
- `owner_compaction_commit.py`: retains lock order and native/source/journal
  fences; compares relevant owner rows and existing DeliveryScope messages.
- Focused tests plus migrated interception points in existing consumer fixtures.
  S1 event families/dispatch, S2 RPC, S3 runtime, S5 counter semantics, S8 goal
  authority, historical sessions and default-on adaptive compaction are retained.

## Failure and fixed semantics

Actual failure source (read only):
`/home/ts/.local/state/agent-comms/pr95-real-queue-20260928.json` and `.log`.
An accepted followup was rejected at native commit by the blanket UNKNOWN check;
whole-ledger revisions would also reject it or unrelated foreign ingress.
Neither uncertain live input nor the operator script was replayed.

A queued item gets its exact persisted receipt only after InputDispositions.record
fsyncs. Receipt publication and queue insertion occur under the existing wire
lock. The component derives eligible future inputs only for this process,
incarnation, admission and original turn. Compaction checks the receipt still
matches the full unattempted row and excludes only that live future input from
its original source. It can then commit and hand back the one-use original
admission. ACP rechecks the queue boundary before the original bind. Native
start remains the sole STARTED transition; the original and followup keep
separate IDs and each executes once.

Steer, clear, send-now promotion, shutdown, owner/turn change, modified/deleted/
already-bound queued row, changed original, relevant correction and unrelated
old UNKNOWN cannot borrow the exception. Restart cannot reconstruct receipts.
Foreign owner input records and foreign conversations do not invalidate this
owner's summary. Native session source/CAS, exact journal ACK and single-use
admission are unchanged. No native or provider protocol changes.

## Completed local evidence

All tests used the existing Python3.14 test environment with `PYTHONPATH=src`,
`-o addopts=''` (no xdist), persistent owned basetemps, shell limits60/165 seconds.

- `core-tests.txt`: 150 passed (ACP/input/selected admission), 28.78s.
- `native-regression.txt`: 39 passed, 100.80s; actual native source/commit,
  cancellation/lock/CAS/journal regressions and queued followup integration.
- `candidate-tests.txt`: 53 passed after PR152, 24.32s; input guards, channel
  delivery, private delivery and prompt queue.
- `final-native.txt`: both final-candidate native queue cases passed, 14.23s.
  They use the actual prepared SDK/RPC and native commit helper, prohibit fetch,
  supply synthetic model streams and exercise public ACP.prompt. They prove:
  accepted_not_started -> linked/committed summary -> strict reopen -> distinct
  original/followup native starts exactly once and in order, with/without foreign
  ingress. They do not substitute for the parent's configured-provider run.
- `final-integrated.txt`: broader consumer run296 passed/8 skipped plus two
  failures in fixtures intercepting the obsolete ACP emission hook. The final
  test-only correction targets InputDrain.emit_input_disposition instead;
  `original-tests-fixed.txt`: all4 goal-original cases pass in1.11s. The entire
  broader suite was not rerun after that test-only hook correction. Eight skips
  require the optional stack fixture; no aborted run is counted green.
- `nra-before.json`, `nra-candidate.json`: full package dependency context,
  79 detectors analyzed, none omitted, complete, zero findings in selected files.
  Ownership is an authored semantic decision and explicit source movement;
  no NRA-native equivalence proof is claimed for component synthesis. Executed
  local/native behavior tests supply that evidence. No CI wait.

Earlier failing extraction/fixture runs are retained as diagnostics, not claimed
passing. A first source-fixture attempt hit a nested lock and timed out; the
component now has explicit caller-locked and locking ledger entrypoints, and
all39 native commit/lock cases pass. No live root or saved session was mutated.

## Parent installation / rollback

1. Integrate production1dea4f7 and the final test/evidence commit. If integrating
   Pascal concurrently, retain his generation API renames and this queue plumbing.
2. Build/install the Python wheel into the parent's isolated candidate runtime.
   No dependency pin or native bundle rebuild: retain the merged PR150 package.
3. Run the parent's fresh acceptance harness in its own fresh project/private
   root. It should see durable queue acceptance, one linked/committed summary,
   distinct original then followup native IDs exactly once, expected reply/facts
   and no unresolved new inputs. Never reuse the earlier UNKNOWN attempt/root.
4. Parent activates serially only after its acceptance. No bus migration or
   saved-history rewrite is needed; InputDispositions version1 remains unchanged.
5. Roll back by selecting the previous Python runtime. Live queue capabilities
   do not survive a restart; persisted UNKNOWN rows remain inspectable and must
   not be automatically replayed. Native/session data is never rolled back.

## Remaining boundary and next work

No implementation blocker remains. Actual configured-provider acceptance passed;
serial deployment remains with parent. Complete S7 TurnRunner ownership is the next
independent surface: preserve this component and SessionLifecycle; no queue,
compaction or native protocol redesign is needed for that work.

Owned caches/basetemps and the read-only native-package symlink are removed after
all test/scan workers exit. Evidence stays committed. No live package was edited
or copied, no provider call or extra worker was started by this implementation.
