# Goal execution is derived from the attempt ledger, never stored

Replaces P386, P416, P448 and P406's separate patches (and #2503) with one owner fix. Read against origin/main 73f321e6e, 2026-10-21.

## The defect

A goal's failure is owned twice in the attempt ledger (`Reservation.fail`, goal_attempts.py:188, advances the generation to `BlockedGeneration`; `FailedTurnEvidence`, goal_failure_observation.py:42, holds turn, scope, owner and reason). The registry's `BlockedGoal(reason)` is a third copy, written by nine paths that bypass `update_goal`. Nothing clears it except Retry, a fresh order or replacement (`BlockedState.successors() == (BlockedGoal,)`), and the scheduler skips non-active stored states, so a stale copy wedges a unit for good.

Writers of the copy (all deleted):

| | Site | Reacts to |
|---|---|---|
| W1 | agent_events.py:281-284 `Done.observe_goal` -> `GoalTurnPermit.block` (once per claim) -> `TurnGoalAccount.block_active_goal` -> `Goals.block_goal_after_failed_turn` (goal_management.py:442-460) | backend `Done(ok=False)` mid-turn, including provider-retried responses (P448: blocked while still running, written three times) |
| W2 | turn_goal_account.py:168-172 `require_output` | success with no productive tool; the ledger records verified progress for the same turn |
| W3 | turn_terminal.py:94-97 `FailedGoalTurn.settle_launched` | unverified terminal; the ledger is written 4 lines earlier |
| W4 | turn_terminal.py:108-110 `EndedWithoutResult.settle_launched` | missing result |
| W5 | turn_terminal.py:57-61 `settle_origin` -> `FailedGoalOrigin` -> `Goals.block_failed_origin` | origin turn failed |
| W6 | schedule_rules.py:119-127 | launch refused by the ledger |
| W7 | goal_actions.py:235-251 `ActiveGoalAction.before_transition` | Resume found a failed generation |
| W8 | (deleted by #2439) | unresolved attempt; its stored rows remain (P416) |
| W9 | goals.py:1046-1051 `Goal.after_failed_turn` | appends the diagnostic into progress |

Kept as stored decisions: `CompletedGoal`, `BlockedGoal` from `BlockedGoalAction` (an owner or model block), `PausedGoal(source)`, `RecordedGoalWait`, and the ledger itself.

## The owner: `GoalExecution`, derived on read

An ABC family replacing the `GoalExecutionState` StrEnum, the `ExecutionPresentation` classes and the `GoalExecution(state, ..., block_reason: str|None)` record. `IdleGoalExecution` stays the null member.

- Decided: `CompletedExecution`, `DecidedBlockExecution(reason)`, `UnrecordedBlockExecution`, `PausedExecution(source)`, `StandbyExecution(wait_for, inactive_wait_for)`.
- From the ledger (an `ActiveGoal` with no wait): `RunnableExecution` (Ready generation); `RunningExecution` (Reserved or Blocked generation and a live turn on a live owner, pid+start_time); `AttemptUnresolvedExecution` (Reserved, no live turn); `AttemptFailedExecution(evidence)` (Blocked, no live turn; reason and turn come from `FailedTurnEvidence`).
- Bases: `BlockedExecution` (Decided, Failed, Unresolved: glyph, Retry toggle, commander action; Unresolved overrides with "inspect the unknown attempt; never replay") and `WaitingExecution`.

Precedence is the delegation chain across the owning records, one member per goal: `GoalState.execution(source)` (decided states answer; `ActiveGoal` delegates) -> `GoalWait.execution` (a recorded wait answers; `NoGoalWait` delegates) -> `GenerationState.execution` (Ready answers; Reserved and Blocked delegate) -> `TurnDeclaration.execution` (a live turn on a live owner answers Running; otherwise Unresolved or Failed). The one conditional is owner liveness, a live OS observation.

A derived block clears itself when the ledger moves: Retry (`authorize_retry`), a fresh order (`authorize_current_retry`), a stop (`permit.stop` -> Ready n+1), or a verified productive turn on a Blocked generation (`authorize_current_retry` with witness `verified-turn:{turn}`).

## How each order disappears

- P448: a failed turn writes the ledger once (`Reservation.fail` is fenced by `StaleAttemptError`); mid-turn retries write nothing; a live turn reads Running.
- P416: a resolved attempt reads Ready, so the goal reads Runnable; edit 10 migrates the stranded rows.
- P386: no registry write on install stop, and edit 8 makes the stop win over the dying backend's `Done(ok=False)`, so the ledger records a stop (Ready n+1) and the unit continues after relaunch.
- P406: same disease at the owner level; edit 9 decides owner loss from the attempt's recorded owner process being dead, not from a release acknowledgement compared against a moving claim counter.

## UnitState (P439, #2488)

`GoalExecution` owns "can this goal's next attempt launch, and if not, why". `UnitState` owns per-unit commander attention. `UnitState.Blocked` reads the goal's derived execution; #2488's `UnitBlock`, `UnresolvedUnitBlock`, `RetryableUnitBlock` and `.accepts` (a second classifier over `goal.state` plus the ledger) are deleted, as is commander/standing.py:96-164's `BlockedHolder` family. Live-turn checks use the same owner-liveness read as `RunningExecution`, never the stored `thread.executing`.

## Edits, one order

1. goal_presentation.py: replace the StrEnum, presentation classes and record with the `GoalExecution` family and bases above; each member renders itself.
2. Add `execution(source)` to `GoalState` members, `GoalWait` (goal_waits.py:76-81, 160-169), `GenerationState` members and `TurnDeclaration` members (turn_lease.py). `source` is one frozen `GoalExecutionSource(goal, wait, generation, evidence, owner)` built in `Goals._goal_snapshot` (goal_management.py:372-377). Delete `GoalWaits.execution` and `describe_execution`'s state lookup.
3. Delete W1, W3's registry write (keep `reservation.fail`, passing the failure reason as its diagnostic), W4, `TurnGoalAccount.block_active_goal` and `require_output`, `GoalTurnPermit.block`, `TurnTerminal.observe_goal` and its call at turn_progress.py:210.
4. `settle_origin` writes only `permit.fail_origin`; delete `FailedGoalOrigin` and `Goals.block_failed_origin`.
5. Delete `Goals.block_goal_after_failed_turn` and its caller (W6); every `after_failed_turn` and `block_after_failed_turn` (goals.py:725-732, 822, 933, 1046-1051); `GoalOrder.failed_turn_progress`; `BlockingOnFailure`; `PausedGoal.after_failed_turn`; `FailedTurnProjection`, its `ObservationState` Literal and `read_failed_turn_projection`.
6. Resume keeps its refusal and drops the `BlockedGoal` write (W7). Retry drops its `isinstance(goal.state, BlockedState)` gate (goal_actions.py:664); `authorize_current_retry` is the gate. The Retry toggle moves to `BlockedExecution`.
7. An unproductive success is downgraded to `ok=False` at turn_progress.py:199-206, beside the unknown-attempt downgrade. `VerifiedGoalSettlement` (turn_goal_account.py:34-57) authorizes a retry when a verified productive turn lands on a Blocked generation.
8. Stop precedence: `TurnTerminal.interrupting(prior)`; `OperatorStopped` wins over a failed `Done`; the worker's `OwnerStop` reaches `finish_goals` (owned_turn.py:321-322) so a stop requested before settlement wins. Delete `interrupted`.
9. Owner-loss proof (P406): `observe_native_release` (attempt_recovery.py:105-107) builds `UndispatchedOwnerLoss` from the attempt's owner process (pid+start_time) being dead and not the registry's, plus no native subprocess alive in its storage files; delete `UnrecordedNativeClaim.require_release`'s exact-claim and idle checks (native_claim_epoch.py:66-70). Settlement stays UNKNOWN, never replayed. This reverses the docstring rule "a replaced PID alone is insufficient"; the identity is pid+start_time and the subprocess check stays.
10. One-shot migration: stored `BlockedGoal` rows whose reason is one of W1-W8's texts become `ActiveGoal`; every other `BlockedGoal` stays a decision. The texts live only in the migration, deleted after it runs.
11. UnitState: `BlockedHolder` (standing.py:96-164) and #2488's `UnitBlock` family collapse into `GoalExecution.unit_blocks()`.
12. #2503 closes as superseded.

## Not determined

Which precondition fires in the live P406 case; whether install signals the Pi child directly (a residual race in edit 8); whether goal execution is republished after a ledger-only transition made by another process.
