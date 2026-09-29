# Queued owner restarts

`comms_queue_restart(name)` records an exact running owner incarnation and starts a
single event-driven watcher for the bus. The watcher waits for registry changes
(no polling or model calls), then uses the existing guarded `restart_owners`
operation **only while that owner is idle**. A concurrent turn claim wins over
a restart. The tool never interrupts a turn or sends a new prompt.

`comms_restart_queue(name)` reports saved requests; `comms_cancel_restart(name)`
cancels only requests that have not been attempted. `pending` means not yet
attempted; `restarted` includes the old and new PIDs; `stale`, `blocked`, or
`cancelled` means no automatic retry; `attempting` and `uncertain` require operator review.
An attempt is persisted before the first possible signal. If the watcher dies
mid-attempt, the request remains `attempting`, rather than replaying it.

The queue is private to the wire root at `owner-restart-queue/`. It records no
credentials, model flags, or session contents. The watcher reads the original
owner's launch environment from `/proc` after verifying its process/socket and
preserves it only in memory for the replacement launch. Requests bind to the
owner's PID, creation time and admission generation. Linux `/proc` and inotify
are required; other platforms fail closed.

This restarts within the **same installed Python runtime**. It is not a runtime
upgrade, deployment switch, or authorization to restart an owner under a
different interpreter. A separate reviewed cutover is required for those.
