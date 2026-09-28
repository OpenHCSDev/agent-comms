# S13 remaining caller/test closure

Fixture migration covers46 files. Thread constructor/replacement sites now carry captured real ProcessIdentity, retain an existing identity, or use None for an unowned record. No synthetic birth constant, PID constructor adapter, or production compatibility was added. The foreign-owner compaction check launches a real different child and retires it in finally.

Tests were three-way merged against parent integration bb1892e so S9's deleted compaction-process helper and current publisher declarations stay deleted. The parent-deleted supervised-cutover test was excluded rather than restored. Queue tests test_acp_queue_contract.py and test_prompt_queue.py were untouched. Parent now owns test_acp_private_nk_delivery.py and test_read_ledger.py; their already-completed fixture changes are included per instruction, with parent resolving subsequent alias/read behavior overlap.

The reported missing test_goal_direct_interrupt helper import is removed: test_acp_owner_interrupt_followup.py binds the current canonical session and real identity itself. Existing followup assertions are retained, including actual RPC child acceptance, but their old direct_interrupt/pending-turn driver still requires migration to the new production followup API. This is not a passing runtime acceptance claim. A separate current canonical probe reproduced production goal_attempt_store_unavailable when fresh owner input arrives during a selected DM; routed to parent229 and Pascal234 (comments5873882575/5873882924). No retired dispatch path was restored.

## Local checks

- Current integrated required guard command:22 passed,2976 deselected,4.33s. See integrated-guards-after-test-merge.log. The missing helper collection error is fixed.
- Previous isolated consistent-source shard:91 passed,8 deselected. See identity-fixtures-core-consistent.log.
- Newer integration registry/lifecycle shard:63 passed,7 failed. See identity-fixtures-registry.log. Failures need current canonical bus/owner semantics, not pid constructor compatibility; parent subsequently owns read/alias cases.
- Goal shard:30 passed,8 failed due missing canonical bus marker in old ACP goal fixtures. See identity-fixtures-core-new.log. Assertions retained.
- Scoped Ruff imports/format and Python compilation passed.
- Failure logs are retained; earlier mixed-version/snapshot collection failures are not claimed as production failures. Integration tests run in our owned .artifacts/integration snapshot, not a shared worktree; no installs or live restarts.

A12 repeated-cancellation, inherited direct-parent launch, Windows, owner lifecycle and recovery behavior remain as previously published; no redundant rerun of completed child acceptance here. Full test caller closure remains open until current native followup seam and remaining bus fixtures are migrated.

## Follow-on closure

Canonical ACP fixtures now install the explicit bus marker and bind their native package/root, retaining the existing stream/event assertions. Goal failure, activity delivery, headless diagnostics, ordinary N/K, foreground N/K and optional awareness focused batch: **88 passed in30.66s** (canonical-fixtures-followup.log). This supersedes the eight missing-marker failures above; it is a fixture/API acceptance result, not provider proof.

Nine remaining fixture modules now derive native runtime input/cursor table names from NativeRuntimeInput/CurrentNativeCursor declarations, use current sent_owner_admission_generation, and read the cursor source through its typed owner. Retired table aliases were not restored. Canonical refusal test now matches the actual missing-marker reason and still asserts byte-for-byte no append.

Known integration dependencies retained:
- Native recovery requires S12's current OwnerReleaseStore adoption: parentbb1892e still calls deleted _read_owner_release_receipts. Current native recovery failures are recorded, not rewritten to pass.
- Current foreground model fake calls prompt_send_boundary once and does not implement the raw writer's pre-send PromptAdmissionBusy retry; the two-recipient cases expose lock contention. Real production/raw writer owner remains S10; no relaxation of no-overlap assertions.
- Passive-awareness tests still target removed acp_passive_channel_awareness.json; migrate to parent's current InputDrain/awareness projection, never recreate that ledger.
- Owner interruption/followup acceptance still needs the current native steering API from S10. Reproducer source is retained as canonical_followup_probe.py with explicit model-fake boundary. Original behavior assertions were not removed.

Cursor/certificate/checkpoint broad shard reached the60s bound (exit124, no final pytest summary), so remains unverified. Owned .artifacts/integration and test artifacts were removed after a /proc environment scan found no process using that snapshot. Failed/partial logs remain as evidence; no worktree source or live roots were deleted.

## Current-parent continuation (26ec262)

Merged current parent229 into own PR232 as fb1a53b. Eight test merge conflicts were equivalent parent literal SQL names versus S13 declaration-derived names; retained declaration ownership. Parent alias/read changes are present and were not reimplemented.

- Rechecked the requested canonical-marker goal fixture:8 passed,35 deselected (current-parent-goal-fixtures.log). ffbc212 already contains that fix after parent's earlier8908647 merge.
- Four further ACP standby fixtures now enroll the canonical marker/package/root. Same-ID resume asserts decoded Goal state through FieldCodec instead of retired status projection. Standby/resume/input-drain batch:57 passed (current-parent-standby-fixed.log).
- Current-parent required guard command:22 passed,2978 deselected (current-parent-guards.log).
- Native owner_fixture no longer assigns a raw asyncio.Process to PersistentPiSession.proc: it launches AttachedChild and asserts reap plus exact identity death on cleanup. test_input_drain_native also enrolls the canonical marker/root/package.
- Real Node selected summary -> native commit -> exactly-once original admission:1 passed (current-parent-native-summary-fixture.log). Read-only prepared S9 bundle /home/ts/wt/comms-refactor2-s9-20260928/stack/.pi-native-50e477b6db64167e/node_modules/@earendil-works/pi-coding-agent; AC_NATIVE_STACK_BIN points at that worktree's stack/bin/pi-native. Native processes/files/protocol/commit are real; model stream is synthetic and outbound fetch forbidden.
- Real ACP queue/compaction test remains RED on current parent: current_prompt_input_missing, exit_code0; owner followup remains UNKNOWN. Cleanup assertions pass. Receipt current-parent-native-queue-current-bundle.log, routed to Pascal234 comment5874224892. Do not infer failure on Pascal's newer unintegrated native route; recheck after adoption. No acceptance assertion removed or native proof invented.

Ownership coordination: Nietzsche239 comments5874100662/5874149117 carry current canonical fixture changes and exact remaining goal-input-review driver obligation. That test still references deleted pending/direct_interrupt APIs; policy/projection must be coordinated with Nietzsche/Pascal before migration. Copernicus238 comment5874134611 names the remaining excluded Thread(pid=...) files. Own source and own fixture batch have no remaining Thread(pid=...) constructor calls. External NativeWitness.pid and attach_session OS-boundary PID inputs are distinct and retained.
