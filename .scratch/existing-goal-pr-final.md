## Delivered

**Backup-first live carry completed: 17 missing generations recorded, zero remain missing.** The temporary outside-src tool was deleted after the run; its working source and preview/carry checkpoints remain in branch history. No runtime repair path was added.

Zero recorded attempts does **not** prove a goal never ran. The new READY row describes only a fresh unreserved generation. Public blocked/paused/completed/archived state was not rewritten. BlockedState still refuses ordinary Active/Resume from Model, Runtime and Owner actors; only explicit Owner Retry authorizes a fresh generation. UNKNOWN input remains untouched and is never replayed.

Existing rows were excluded. paper1-editor still had its generation 2/BLOCKED with two attempt records and executing turn at readback. Each missing-row write rechecked the exact current goal, owner incarnation, absent generation and absent attempts under wire custody. SQLite backup occurred **outside wire/registry locks**; no bus copy occurred.

Coach source 3885's disposition is recorded before live carry: the earlier UNKNOWN Bash admission was explicitly abandoned, never rerun; fresh distinct commands and backup-first live carry were authorized. Shepherd's read-only preview review was not used as write approval.

## Opened-store consumption

OwnedTurn.stream now calls `Goals.open_goal_store()` through `Coordination.run_worker`. Its sole `OwnedSendAdmission` constructor cannot pass the nullable cache. Admission's field is non-null and its redundant assertion is removed. Cache renaming/lifecycle follow-up is deferred. No acquire/finish/terminal-claim changes overlap #780.

## Source and behavior evidence

- Existing refactor-audit Package parser: before/after **307 production modules, zero omissions**; after **359 test modules, zero omissions**. The sole external nullable-cache reader was OwnedTurn.stream; it is deleted. All nullable cache reads/writes now remain in Goals. Python evidence does not prove compiled JS/dependency dynamic binding.
- Offline operator: three carried, repeat zero; public registry/input bytes and existing attempted row unchanged. Ordinary Resume refused for all three actors; explicit Owner Retry advanced the same goal to generation 2 with UNKNOWN unchanged.
- Focused serial batch: **34 passed**. Initial default pytest had 33 passing tests but failed the project-wide coverage threshold; no full-suite PASS.
- Clean-export candidate installed with current Toad #606 dependencies: real Toad App/Pilot → ACP/native owner → saved SDK history and blocked historical goal → actual Retry widget refused missing authority with **zero provider calls** → fresh carry row still blocked → actual explicit Retry widget click completed the same goal at **generation 2**. Two localhost controlled responses/two saved inputs, original history prefix preserved, native child retired. This is installed headless UI acceptance, not paid-provider or new physical acceptance.
- First candidate UI check timed out matching text beyond a clipped single-line error; zero provider requests. Original retained; successful check used a fresh session/root, not replay.

Full record: `docs/goal-generation-carry.md`. Backup/receipt: `/home/ts/wt/core-doctrine-segment/.scratch/existing-generation-live-backup-20261013/`. Candidate receipt/SVGs: `/home/ts/wt/core-doctrine-segment/.scratch/existing-generation-installed-journey-v2/`.

Prior accepted #777 physical st receipt/screens reviewed by shepherd: `/home/ts/wt/core-doctrine-segment/.scratch/goal-physical-20261013-final/physical-final-20261013-191/`. That path exercises new goal creation, not historical carry.

**State:** live data carry complete; store-consumer source ready for exact-head review/merge authorization. Not merged or installed into the live runtime yet. CI deferred.
