## Goal generation carry — first checkpoint

Follow-up to merged #777. One-time tool outside `src/` currently previews the live registry and private ledger read-only (SQLite URI `mode=ro`, query-only, no writer/store initialization). It prints the exact goals it would carry. It will be extended to back up the ledger before fenced writes through the existing GoalAttemptStore, run once, and be deleted before ready.

Initial live census: **17 goals have no generation row and no attempt records**. This includes codex-shepherd's blocked goal. Public status, text, revision, owner and UNKNOWN/input history will remain unchanged. Public block/paused/completed status is distinct from an unreserved private READY generation; explicit Retry remains the existing owner decision.

**paper1-editor already has generation 2, BLOCKED, with a recorded attempt and two attempt records.** It is not a missing-row carry candidate; its executing turn/attempt will not be modified or replayed. The carry will never replace an existing row.

Remaining implementation: backup-first carry with current goal/owner and absence rechecked before each write; isolated old-goal Retry acceptance; OwnedTurn.stream must pass Goals.open_goal_store(), not the nullable cache. Cache renaming is deferred per latest coach instruction. No production change or live carry write at this checkpoint.

Source owners: RegistryStore/FieldCodec decode public facts; Generation/AttemptRecord own ledger reads; GoalAttemptStore owns creation and durability; ReadyGeneration owns explicit Retry recovery. The temporary tool will not add a runtime repair path or invent an attempted failure.

Prior physical #777 acceptance capture/receipt: `/home/ts/wt/core-doctrine-segment/.scratch/goal-physical-20261013-final/physical-final-20261013-191/`. Saved native history → actual st keyboard input → model set/block → actual mouse Retry → same goal completed at generation 2; five controlled localhost requests, three saved inputs, original history prefix preserved; children/terminal/display retired. Failed originals retained separately. This does not yet verify the historical carry.

Stopped at draft pending completed implementation, retained carry count/backup receipt, focused and actual installed acceptance, source review and exact-head census.
