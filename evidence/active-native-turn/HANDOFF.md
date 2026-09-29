# S2/S13: active native turn cleanup ownership

Base: main through #325, `6bd8c423`. Owner: native backend sidecar; parent owns install.
No overlap with Boyle #331 automatic response production or Dalton #330 reply proof.

Read the original `plans/nominal_refactor/files.zip:S2-pi-rpc-boundary.md`, round2
00-RULES and S13, both latest skills and pattern catalog. The original S2 groups
steering lifetime with the actual turn; S13 requires existing child supervision.

## Decision and deletion

IDEN-5 / IMPL-13: backend still tracks the same turn through three global maps:
child process, stderr task and steering task. Those copy references already owned
by TurnSession/PiSessionChild and force independent registration and cleanup.
Replace them with one TurnSession index containing actual session owners. The
existing stop command resolves the owner then stops forwarding and retires its
existing native custody. No process wrapper, copied handle, new reader capability,
registry facade or second stop algorithm. A12 still stops/reaps the child.

Delete every registration/projection of the three old maps, including MessageStart
and direct test callers. Keep the public termination command used by owned_turn,
turn_runner and cold preparation; its task lookup is its real domain operation.
Those caller files need no edits. Existing custody owns cancellation-safe retirement.
The run finally block stops forwarding, retires nonretained custody and unregisters.
Successful retained children stay alive; no original or pending input is replayed.

Runtime in-memory indexes only; no persisted format or migration/reset change.

## Acceptance underway

Actual pinned native cancellation, external owner termination, EOF, early failure,
queued input/reuse and cold preparation; focused recorded cleanup/error cases.
Check actual child death, stderr/steering task completion and absence of stale
registration; cleanup must never reap another turn's child. R0 existing class sizes
and touched-file chain terms may not grow. Preserve all failed receipts.

Initial coherent patch: 38 production lines deleted, 23 added (pending measurement).
No readiness claim yet. No CI wait, no paid calls or live changes.
