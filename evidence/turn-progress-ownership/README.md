# S7/C0 turn observation and goal-account closure (PR323)

Ready source: `8f433050906e835c74cf03056224181f63047a01`, merged with main `8ad034a6`. Corrected318 `9461917c` is included. Parent owns merge/install; no live route or shared root was changed. No diagnosed source/caller blocker remains for this slice.

## Ownership and deletion

**494 production lines deleted, 621 added** across six production paths; tests delete20/add165. Net production +127 is explicit dependencies, state/proof owners and composition, not a net line reduction claim.

Original S7 section5/step5 and C0 require components to own facts rather than forward through a shared execution object. TurnProgress held OwnedTurn and mutated its goal/terminal scratch fields through execution.runner. The replacement owns observed event and terminal state directly. TurnGoalAccount owns live permits, originated attempts, usage and settlement; the existing GoalAttemptStore SQLite remains durable authority.

- Delete TurnProgress.execution/runner capture, finish_attempts, duplicated originated-goal-ID set, and TurnRunner._goal_attempt_unavailable. Existing ACP refusal details move with reservation ownership.
- Migrate OwnedTurn composition, stream callbacks and finish caller, S1 event fixtures, and ordinary input callers. No compatibility constructor, alias, reexport or old caller remains.
- ChannelInputBatch captures admitted channel identities/text once and compares that snapshot with the proposed input. SingleInputBatch owns refusal of multiple keys.
- InputDependency variants own captured-wait versus absent-dependency behavior. DirectInput shares actual direct-input permission; OwnerOriginalInput owns saved-session compaction eligibility. Final augmented prompt is copied into the immutable original source before streaming.
- VerifiedGoalSettlement and OriginGoalSettlement share actual GoalState MRO dispatch, with different witnesses; FailedGoalOrigin preserves paused state. TurnEventPublication declares post-event synchronization once, including tool completion.
- Existing wire lock through native write, exact key/revision/owner fences, refusal versus deferral, backend constructor and close_idle boundaries remain intact.

This is live transient accounting/proof state over unchanged durable inputs, registry and goal store. No stored format, migration or codec is added. Dalton prepare/result/error-feedback and Wegener native internals remain their authorities; current main integration is included.

## Latest skills and patterns

Reread nra-refactoring and actual22:16 refactor-audit from `/home/ts/.local/share/agent-comms/skills/refactor-audit-20260928-2216/refactor-audit`, including pattern README, implementation IMPL-14 semantic classification and scripts/audit/chain_terms.py. Earlier full implementation, over-time and agent-defaults reads covered IMPL-4/5, TIME-9 and AGENT-8.

- IDEN-1: old OwnedTurn179 six-term batch check becomes an exact batch snapshot.
- IMPL-10: old OwnedTurn290 eligibility flag chain becomes the original input capability.
- IDEN-3: dependency absence and goal-account facts are owned by their live state owners.
- IMPL-4/5: goal lifecycle dispatch is shared by ordinary/origin settlement, including subtype inheritance; post-tool sync is declared once.
- IMPL-8: explicit stateful owners replace writes through captured orchestration.
- IMPL-14: classifier result used to choose underlying authority; no rule-per-boolean expansion.
- TIME-9: no adapter, new wire form, scalar wrapper or second codec; existing typed values remain authorities.
- AGENT-8: run actual current census/classifier and packaged ratchet; no custom replacement audit mechanism.

`chain-classification.json` records original chain kinds. The three TurnRunner chains in that record are existing scheduler/selected-handoff code outside this slice; the only runner change deletes the dead reservation error helper. This receipt does not claim all S7 or all repository debt is closed.

## Behavioral evidence

All tests use the installed noneditable wheel in this worktree's isolated Python3.14 environment. Native tests use the installed Pi package at `/home/ts/.local/share/agent-comms/native-current-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent` with local HTTP fixtures, real child processes/history and no paid provider calls.

| Receipt | Result and boundary |
|---|---|
| main-current.log | **19 passed,2 deselected,25.74s** after main8ad034a6 merge: S1 real/native event settlement except unchanged manual bridge; goal-account SQLite tests; channel-batch proof; ownership guards |
| native.log | **2 passed20.85s** actual native success200/failure503 through current consumer and settlement, one provider call each, child reaped |
| input-proof-final.log | **6 passed17.85s** channel admission proof, owner guards, ordinary input rules and actual selected-summary handoff |
| goals-current.log | **34 passed,4 deselected14.49s** goal input/original authority, goal retry, admission rules and event effects |
| final-account.log | **14 passed,4 deselected7.38s** SQLite settlement and current event effects |
| final-order.log | **4 passed1.88s** final tool/sync ordering and ownership guards |

Latest command: `PI_COMPACTION_TEST_PACKAGE=<package above> .venv/bin/python -m pytest -o addopts='' --basetemp=.test-main-current tests/test_s1_event_behavior.py tests/test_turn_goal_account.py tests/test_channel_input_batch.py tests/guards/test_turn_progress_ownership.py -k 'not manual_bridge' -q`.

New-case proof: SpecializedPausedGoal inherits actual paused settlement behavior for ordinary and origin accounts without adding any consumer branch. Real SQLite tests also cover a failed origin launch claim: generation retired, registry blocked, pending origin cleared. Batch proof denies duplicated/nonpositive/direct/reordered origins, changed text and missing ledger rows. S1 assertions now check real current goal signature and durable wait/reply effects; deleted callback-count assertions no longer pin the old collaborator structure.

Red receipts retained: progress-tests.log (11fail/2pass, interrupted after callback-name collision); focused.log (8fail/24pass/4deselected, stale private-collaborator assertions); input-proof.log (2fail/1pass, missing new imports); goal-account.log (2 fixture failures: root mode and incorrect expected pre-retirement lifecycle). Each diagnosed issue was corrected and exercised by green receipts. No whole-suite green claim. CI is deferred.

## Structural and source evidence

`census-main` compares8ad034a6 with8f433050: eight long chains removed, **34 chain terms removed** (OwnedTurn10,TurnProgress24), no touched-file chain-term increase. No added codec subclass/type-identity/raw-key/attribute-by-name measures. One fewer long function, six fewer isinstance calls. `ratchet-main.json` has **zero positive existing-measure deltas**, including per-class size. New owner classes are covered by permanent guards. Ruff and diff-check pass.

NRA before scan: full238-file source context, selected OwnedTurn/TurnProgress, two semantic_mirror_without_descent findings. Final scan: full240-file context, six owned production paths, one pre-existing unmodeled_record_shape for TurnRunner.__init__ os.environ settings. Scopes differ, so counts are not a global trend claim. Raw graphs are compressed alongside receipt. The CLI emits neither complete nor omitted-detector counts; this is scoped source evidence, not a complete-detector certificate. No codemod/proven trajectory claim; semantic edits were authored and behavior verified. Final merged main changes no323-owned production bytes relative to that scan.

## Remaining

No diagnosed blocker in assigned closure. Parent review, merge and live installation remain. Unchanged scheduler/selected-handoff predicates and other plan slices are not hidden inside this completion claim. Owned disposable test roots and environment are removed after publication; committed raw receipts and source remain.
