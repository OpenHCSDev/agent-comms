# P520: goal answers and launch claims

Base: `2bfdd00ed` (P398). Neither rejected P520 nor the later P398 typing edits are included.

`GoalDeclaration.project` selects the actual goal once. The overlapping perform, collect and test methods are deleted. Callbacks receive `Goal`, not a declaration that callers must check and cast. Empty work collections and false operation results do not return a nullable goal. Projections require both constructors; instructions are zero or one actual instruction strings, never a dummy empty string. Settlement accepts the settlement class. Action declarations carry their result type, so creation/edit callers receive `Goal` and clear callers receive `NoGoal` without another cast.

`GoalTurnPermit` distinguishes `NoLaunchPermit` from a claimed `LaunchPermit`. The permit controls settlement, blocking, charges and goal context. No nullable permit is carried between the turn, ordinary send and settlement code. The unused unverified-completion operation and its unused state hooks are removed.

Before evidence: `/home/ts/.cache/agent-scratch/core-runtime-location/p520/before-complete.json`: 356 parsed production files, 3,106 classes, 4,140 goal-related declaration/write/import/decision/call sites, zero parse omissions. The original size-filtered evidence file is superseded. The corrected collector uses NRA parsing/indexing, includes long nodes, records parser failures and hashes every covered file. Dynamic callback targets require semantic reading; this is not a behavioral proof.

After evidence: `p520/after-complete.json`: 356 files, 3,107 classes, 4,139 goal-related sites, zero parse omissions. No size cutoffs.

All goal `is_set` calls are removed. Six unrelated Event calls remain unchanged. The seven new projection/cleared-mid-turn cases and existing block/extension checks pass: 25 tests. The six turn cases cover original and in-turn-created goals with successful, failed and missing terminal events, using real registry, lease and SQLite grants. The first broader batch exposed obsolete public-root/native-package fixtures and old relationship mapping assertions; that broader batch is not passing. Shared borrowed dependencies were retired by the runtime update; the check driver now reuses existing cached wheels without creating a venv or installing packages.

The combined change against `2159de33d` is +1,072/-874 source lines (net +198); no budget increase was added. Restoring unchanged import layouts removed 66 lines. `perform` is gone from permits too: their `check_grant` owns the real validation. Cross-type settlement/blocking calls are public, and request creation still prints its explicit missing-request message. The full debt check and a new copied-application run remain unfinished. Earlier P398 set/pause/clear/restart screenshots are under `/home/ts/.cache/agent-scratch/core-goal-retry/p398c/ui-clean-environment/terminal/` and predate these changes.
