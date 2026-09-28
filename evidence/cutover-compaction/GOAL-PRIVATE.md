# Exact goal-private inventory and cutover disposition

Read-only inventory: `/home/ts/.agent-comms/goal-private/goal_attempts.sqlite3`,
2,686,976 bytes. The directory contained only this database at inspection.
No root-level goal_attempts.sqlite3 is used by current TurnRunner.

| Table | Rows | Disposition |
| --- | ---: | --- |
| metadata | 1 | Retired schema marker; offline snapshot only |
| goals | 55 | Preserve IDs/generation/state evidence offline; discard runtime ready_digest grants |
| attempts | 667 | Preserve IDs, token evidence, phase, progress_witness and resolution offline; never reconstruct launch capability |
| human_decisions | 29 | Preserve owner decision history in offline SQLite snapshot; do not replay decisions or mint new grants |
| provider_usage | 7,651 | Preserve accounting/usage history in the same snapshot; never delete without retaining it |
| failed_turn_observations | 1 | Preserve exact failed-turn/owner/generation/reason evidence offline |

Observed goal states: 7 ready, 7 blocked, 32 cancelled, 9 completed.
Attempt phases: 7 failed, 59 resolved, 601 succeeded. No reserved/claimed attempt
was present at this inspection; the install must capture its quiet-time snapshot.
Do not count the seven ready rows as authority to restart a goal.

Parent install contract:

1. Under the parent's quiet boundary, include the nested database and any `-wal`
   in source-revision checks, then take a complete SQLite backup to offline
   precutover evidence. Check reopen equality/integrity before discarding runtime.
2. Remove ONLY the installed runtime database path
   `goal-private/goal_attempts.sqlite3` and its `-wal`, `-shm`, `-journal` sidecars.
   Do not use a recursive whole-directory deletion; preserve any additional
   diagnostics/files found at the actual quiet install. Keep the original root backup.
3. Do not reinstall the old schema, ready digests, attempt tokens or process-local
   launch capabilities. Current runtime creates current schema only on explicit
   owner work. Without a new launch grant, automatic goal scheduling must continue
   to require explicit Retry rather than recovering a prior ready row.
4. Preserve active goal declarations/IDs and durable `goal_history.sqlite3` via the
   parent's registry/history converter. Those are distinct from the runtime grants.

Current callers confirming the exact path: `TurnRunner.open_goal_store`,
`TurnRunner.schedule_goal`, `ReplacementGoalAction.before_publish`, and
`OwnedTurn` in src/agent_comms. This inventory adds no runtime or goal converter.
Parent owns current_root/install_root integration, evidence placement and activation.
