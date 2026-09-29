# S2/S13: active native turn cleanup ownership

Base: main through #325, `6bd8c423`. Owner: native backend sidecar; parent owns install.
No overlap with Boyle #331 automatic response production or Dalton #330 reply proof.

**Ready:** production `a099675f`, final behavior/guard checkpoint `cc61d106`.
**35 production lines deleted, 22 added; executable code -14.** No new production
class, wrapper, handle record, codec or lifecycle flag. TurnSession class size is
unchanged; **25 test lines deleted, 78 added**, chiefly the actual interrupted-stop
proof missing from the old maps. R0 has zero increases and touched-file chain terms do not increase.

## Actual results

- `native-second.log`: **6 passed in 24.54s**, actual pinned native with localhost
  HTTP only. Queued input settlement, retained reuse, >2 MiB output, validated
  reopen; cancellation, EOF and explicit owner stop; cold preparation without
  admitting input; cancellation-safe child retirement.
- `cleanup-final.log`: **7 passed in 11.26s**. Four actual native interruption
  cases plus three declaration/ownership guards. New case interrupts the external
  stop command while actual child cleanup is pending: the session remains indexed,
  the existing retiring state retains its cleanup task, release completes actual
  child/stderr/steering cleanup, the index is cleared, and only one input/provider
  request exists. No retry or replay. The interrupted-turn family also asserts
  both actual background tasks are finished, not merely that a registry is empty.
- `recorded-first.log`: **11 passed, 180 deselected in 7.83s**. Focused cleanup,
  cancellation, malformed RPC and persistent-child behavior. Recorded protocol
  coverage is distinct from the actual native receipts above.
- `lint.log`, diff check: pass. `measurements.json`: no R0/class-size increases,
  no added chains/codecs, six fewer absence checks. The production patch did not
  change after these measurements; final changes strengthen tests/guards only.
- Full-context NRA baseline: **243 files, 40.364s, 107 raw findings**; gzip receipt
  retained. Applicable raw leads are the existing `_tool_title` unmodeled external
  argument shape and `AgentCommsCompactionProgress.sequence` boundary type check.
  Neither is a copied active-task authority or this assignment. That native
  progress boundary validation stays intact. CLI omits scan_status and analyzed/
  omitted detector counts; this is not a zero-omission claim. Ownership decision
  and source patch were authored, not mechanically certified as a DSL rewrite.

`native-first.log` is a retained command error naming a nonexistent test file;
no tests ran. Corrected actual-native receipts above are the acceptance evidence.
No unrelated matrix rerun, provider credits, live restart or installation.

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

## Acceptance contract

Actual pinned native cancellation, external owner termination, EOF, early failure,
queued input/reuse and cold preparation; focused recorded cleanup/error cases.
Check actual child death, stderr/steering task completion and absence of stale
registration; cleanup must never reap another turn's child. R0 existing class sizes
and touched-file chain terms may not grow. Preserve all failed receipts.

No known remaining failure in this assigned cleanup scope. Parent owns review,
paired installed acceptance and deployment. No CI wait.
