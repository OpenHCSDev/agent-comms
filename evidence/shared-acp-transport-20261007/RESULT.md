# Agent tab connection reuse

Core707 and Toad518/519 are merged. The corrected reviewed build is the default for new TUI/client launches; running native owners were preserved.

A real installed same-project tab reused the original ACP child. Initialization took0.014ms, view switch383ms, saved load170ms, agent ready723ms after pointer start. Saved native history was visibly published. This is one run and not input-to-photon timing; the previous default spent1.7–1.9s initializing a separate connection. Cold/different launch contexts still start independently.

Two installed checks passed (8.43s): actual stdio attachments with independent permissions and an actual native App with the eager scheduler. The physical close/reopen sequence remains incomplete: recorded close geometry changed and the click did not close the tab. No whole-journey success claimed. Original owner unchanged; recorder cleanup left no owned processes.

The first recorder failure exposed a missed task-owner consumer. The next run exposed an eager-start race. Both negatives remain intact; defaults were restored during repair. The acquisition owner now assigns the connection before scheduling tasks. No inputs/providers were submitted or replayed.

Exact live result: `.artifacts/sidebar-live-candidate-20261006/shared-acp-fixed-wheel/live-result.json`; raw: `/home/ts/.cache/agent-scratch/shared-acp-fixed-default-live-20261007/`. Disposable archive build sources were removed after clear borrower checks; installed prefixes, wheels and raw evidence remain held.
