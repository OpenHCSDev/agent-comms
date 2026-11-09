# Registration / Goals startup repair

Hand off `goal-fork-handoff.plan.json` for application **after friction's current rebase**, with normal native application reproof on the saved rebased tree. No shared-tree application or edits were made here.

**Three native-clean stages, five operations, exit 0.** Plan SHA256: **91e9faf26ae2f921dc1fe61446ae568a179767a78c61595a0100b85dd682a18b**.

The named failing input is Core **27e59c079710b9d5405725edb82d3f6fe1a9a047** and `p23-startup-after-selected-owner-{core,toad}.log`: Registration imports goal_management, which reads GoalAction while goal_actions is still initializing GoalRecords.

Registration's three child-registration methods only pass the supplied goal provider to the original `execution.child_goal` callback. Both `SubagentThread.child_goal` and `ForkThread.child_goal` already declare the exact lower contract **GoalFork[Thread, GoalDeclaration]**. The real **Goals already inherits that contract** and retains its original fork_goal implementation. No new abstraction or provider is needed.

The cut imports the existing GoalFork, changes only the three parameter annotations and removes the now-unread goal_management module import. The existing goals module binding remains for the original GoalDeclaration value type. `registration.diff` is the complete source change. The native author also compares every Registration procedure body, concrete default and non-method declaration before/after: all match. Child preparation, original callback behavior, actual Goals/Thread/Goal records, declaration/store transactions and acquisition order are unchanged. No GoalActions/GoalRecords edit overlaps friction's independent repair.

The retained selected-owner projection was reused instead of rebuilding another archive. All **1,620 Python files** and pyproject.toml were checked against exact Git blobs in 27e59c079 before native preflight; see `source-input.json`. Reconstruct that input from the previous selected-owner artifact's archived 7b480b211 source plus its already-applied native plan. The native proof is pinned to that pre-rebase source; it is not a proof of the rebased combined tree.

No suite, standalone startup or installation was run. Friction owns actual application and continued shared startup. Do not replay the earlier selected-owner plan: that cut is already applied.
