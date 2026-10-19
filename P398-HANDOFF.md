# P398 stopped after order 30795

P398 was removed from core-goal-retry at 16:26 EDT. No further source edits are authorized here.

## Published work

- Core PR 1472 still points to 2bfdd00ed919917cc20865529fda6362bdffb019, branch fix/p398-crl2.
- Toad PR 963 still points to f672d0f5, branch fix/p398-goal-display.
- Neither PR was merged or installed here.
- Last committed Core debt check exits 1; Toad debt exits 0.

## Rejected, uncommitted work

`git diff` contains 15 Core files, 128 additions and 176 deletions at the last count. None of this batch was committed or pushed. Do not merge it as a completed repair.

Order 30794 rejected the per-caller `require()` additions after `is_set()`, repeated record extraction inside expressions, and TurnGoalAccount's new NoGoal return for missing permit / goal-ID None condition. Those changes remain in the working diff for the next owner to remove; P520 belongs to core-runtime-location. The order requires a typed Goal parameter where a function already holds a record, and a separate no-permit type.

Other changes in the same diff: GoalDeclaration carries default empty display behavior; document/control visibility derive from visible; unused require_order_slot definitions are removed; execution owns details-heading refinement; five new local imports and two old TextDigest local imports moved out of functions; dependent annotations use qualified module references. turn_context's goals import is currently below its provenance declarations to close the actual import cycle. This still needs review, not an assumed green result.

## Verification

- Current dirty source import and GoalDeclaration encode/decode round-trip: exit 0, `p398c/import-cycle-fixed.log`.
- Current dirty focused test command hit its 20-second tool timeout after printing 34 dots / 100%; no final exit receipt was written. Do not report that command as a passed run.
- Previous committed focused run: 34 passed, `p398c/final-tests.log`.
- Previous committed source UI with cleaned live-root PATH: set -> physical Pause -> physical Clear -> restart to Ready, driver 0, restart 0, stop 0. `p398c/ui-clean-environment/result.json`; process identities 3213297 -> 3216669, both exited. Four screenshots in that root's terminal directory. This predates the rejected typing batch.
- Driver now constructs PATH from the immutable runtime bin, /usr/local/bin and os.defpath only. No run was started with that latest driver change.

Evidence prefix: /home/ts/.cache/agent-scratch/core-goal-retry/. Own unchanged native-package copy under stack is needed for a source run; no installed runtime/package was changed. Preserve copied sessions and all failed/UNKNOWN roots.
