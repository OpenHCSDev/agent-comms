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

Evidence in progress: first focused run26passed/6failed; four lacked explicit
native package env, two obsolete watcher fixtures had no registered/bound owner.
Failures retained in source-first.log. Fix fixtures and run bounded corrected
checks; installed saved-native/ACP goal-failure/explicit-Retry wake journey still
required. This draft is not live-ready. Parent active native/UI gate unchanged.
