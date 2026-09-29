# PR318 — ordinary send admission ready

## Ownership and deletion

Based on main a1d24d3a (including corrected312,311,314,315 and deployment317), rebased without source conflicts. Parent owns integration and installation.

Deleted `OwnedTurn.send_boundary`, `input_keys_valid`, `native_start` and all callers. `OwnedTurn` loses247 class lines. `OwnedSendAdmission` captures actual turn/input authorities; input source, goal permission and ordinary/selected binding declarations own their behavior. No captured runner, compatibility methods, parallel registry/store, wire-format change, or live runtime edits.

The early review correction is included: `OrdinaryOwnerCheck`, `AcceptedInputCheck`, context/journal/grant checks extend the existing `ReservationRule`/`RuleCheck` family. They reuse `ThreadIncarnation`, `ProcessIdentity`, `TurnId`, `QueuedInput.current`, `GoalWait`, the goal-attempt store and actual selected admission capability. Owner and receipt failure always refuses. Deferral is computed only after these authorities pass. Named violations remain in logs until the native callback maps to True/None/False. Durable binding exceptions propagate. The same wire lock spans the callback yield through native stdin.write.

Ordinary active-turn semantics remain distinct from private registry admission: same TurnId is required; unrelated active-turn metadata equality is not newly imposed. Canonical alias resolution is preserved.

Wegener's backend/catalog ownership and Dalton's typed manual compaction results/ACP production callers are untouched. `OwnedTurn.prepare_native` is unchanged.

## Executed installed/native acceptance

Installed this source as a wheel in this worktree's `.venv` using `uv pip install --python .venv/bin/python --reinstall-package agent-comms .`. The tested production source is the corrected8b513b2d source, preserved unchanged by the rebase onto a1d24d3a. Subsequent changes only migrate/add tests and retain receipts.

Native package for both environment variables `PI_COMPACTION_TEST_PACKAGE` and `AC_NATIVE_COPIED_PACKAGE`:
`/home/ts/.local/share/agent-comms/native-current-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent`.

- `native-summary.log`: **6 passed,59.95s**. Actual pinned Pi children, native RPC and loopback HTTP provider through ACP. No paid provider call. Exact cases from `test_selected_owner_compaction_integration.py::test_acp_selected_summary_handoff_uses_final_prompt_once`: summary-unchanged-ordinary, decline-unchanged-ordinary, summary-correction-ordinary, summary-future-queued-ordinary, summary-queue-revoked-ordinary, summary-future-queued-private. Proves original once, clean decline, correction refusal, future queue retention and revocation without sending the original.
- `authority.log`: **41 passed,3 stale assertions failed,33.19s**. Includes the entire `test_selected_owner_followup.py`, notably actual native selected+fresh-followup using two native children, local HTTP, real queued receipt and exact input lifetime. Also includes selected handoff owner revocation cases, runtime goal retry, reservation rules and ownership guards. The3 failures were all old expectations of `unknown` for unsent reserved inputs; resolved below. This log is retained red, not called wholly green.
- `goal-rules.log`: **13 passed,9.51s**. `test_acp_goal_input_authority.py`, `test_acp_goal_original_input.py`, `test_compaction_send_admission.py`. Migrated obsolete ACP metadata to current request/update declarations and disabled adaptive compaction only in these ordinary-admission cases. Refused unsent inputs retain reserved/no-native-binding assertions. Exact named accepted-authority reasons are checked. A real SQLite journal intent produces False without a new goal and None after goal activation; neither binds the input or changes the journal.
- `owner-rules.log`: **1 passed,0.90s**. `test_ordinary_admission_rules.py` obtains a production-created owner/turn, exercises each of six named owner fences separately, verifies alias and non-authoritative turn metadata behavior, and attempts an actual second nonblocking flock while the callback is yielded. Acquisition fails inside the callback and succeeds after it exits. Its event producer is controlled; it is separate from the actual native cases above.
- `final-guards.log`: **8 passed,1.19s**. `tests/guards/test_owned_send_ownership.py`, `tests/guards/test_private_send_ownership.py`, `tests/test_reservation_rules.py`. No old methods/captured runner; named dispatch persists; new module/method limits pass; shared family discovers policies.
- `lint.log`: changed-source/caller lint passed. `git diff --check` passed.

Pytest commands use `.venv/bin/pytest -o addopts='' --basetemp=<owned persistent test directory> -q <cases above>`. Native variables must be set; `missing-package-env-red.log` and `updated-baseline.log` preserve early missing-env setup failures.

## Main-baseline failures and their limits

The earlier broad focus ran turn_runner, ACP goal input/original input, compaction send admission, owner_compaction_adaptive, selected_owner_followup, reply_routing and runtime_goal_retry_running. `focused.log` and `main-baseline.log` have the same23 failing test IDs and39 passes on branch and unmodified main0931c47d respectively. Baseline was a separate installed wheel from an owned `git archive` source copy, not a changed shared checkout.

`updated-baseline-with-package.log` specifically reproduces the3 stale goal state assertions on unmodified main after migrating their ACP fixtures:3 failed,2 passed. The corrected assertions then pass against the branch, preserving no-send/no-native-binding and replay checks.

Remaining red baseline tests outside this slice concern old fake adaptive-compaction helper commands, stale native_id/state fields, obsolete error-feedback assertions and old reply-routing setup. These have been reported on PR318 to the parent and Dalton; the actual native compaction acceptance above passes. No whole-suite green claim. CI deferred as instructed.

## Architecture evidence

`ratchet.json` compares a1d24d3a to tested source72497ef7: no positive existing-measure delta; OwnedTurn class size -247; long Boolean chains -8. New methods <=100 lines, modules <=1000. Ratchet counters are structural evidence, not behavior proofs.

`nra-final.json.gz` retains the full JSON scan of the6 changed production modules with explicit `--context-root src/agent_comms`, full raw-findings payload, single parse/analysis worker and120s budget. Source index contains all238 package Python files; completed30.947s,0 emitted findings. This installed NRA output does not expose scan_status or analyzed/omitted detector counts, and contains no supporting_raw_findings field; detector omission freedom or full R1 proof is therefore not claimed. No raw mapping/redundant-type pair was emitted for this slice. No findings is not an equivalence certificate. Ownership changes were authored patches, verified behaviorally rather than represented as a proven codemod trajectory.

## Remaining scope

No diagnosed source/caller blocker remains in this ordinary-send slice. Parent merge/install remains. Broader baseline fixture debt and unassigned OwnedTurn orchestration outside send admission remain separate work. No live installs performed here.
