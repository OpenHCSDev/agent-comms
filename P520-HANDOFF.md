# P520 — 2026-10-18 20:15 EDT

PR1604 has current main merged normally. All twenty callback callers and three generic project definitions are deleted. Both review corrections are fixed. Source +235/-156, net +79; existing Src-Budget:80 declares the actual cost. Checker and baseline are unchanged. The unchanged checker against origin/main exits0 with no positive deltas or introduced findings.

The comparison helper is deleted; callers use ==/!=. One shared operator preserves comparison of the same turn across the different lease/finished record types. Peer fields use NoGoal rather than nullable fields and preserve their original JSON through FieldCodec.

Local checks pass: real-wire set/edit/replace/clear and retirement; both permissions/scopes/contexts/executions/peer changes; passive absent failure; complete coordinator schema and actual declaration-owned SQLite validator registration. Six existing clear/account function cases, absent iteration and cross-record equality ran directly. Pytest is unavailable in the permitted environment. Source/receiver evidence uses the existing NRA AST driver.

Private runtime T000114Z (Core30313, Toade029) showed goal set active rev1, change blocked rev2/progress and physical Clear. Backend became NoGoal and the original generation was terminal CancelledGeneration. One fresh no-goal DM replied with its exact token. Four screenshots reviewed. Copied owner stopped0 before auth deletion, auth absent, original hash unchanged, fresh input COMPLETED and never replayed. No default switch.

## Broken
centre status stuck on Finishing turn after the reply while the sidebar shows Ready (plain/no-goal-reply.png)

Two views disagree about the same turn. The integration owner needs to assign this UI repair. The run and screenshot are preserved under `/home/ts/.cache/agent-scratch/core-pi-durable/p520/goal-ui-complete/`.

Next: merge PR1604 after the exit0 check, give Codex the merge for installation, then use a fresh copied run for installed-launcher goal set/change/retire. Never reuse the private run's input or output directory. P436 waits for its reissued order.

Evidence: `/home/ts/.cache/agent-scratch/core-pi-durable/p520/operations-main-{smoke,named-absence,clear-cases,debt}.log`, operations-main-debt.exit, operations-merged-ast.json, operations-pr-body.md and goal-ui-complete/{result.json,input.json,checks.txt,commands.jsonl,plain/*.png}.

Comms sends at 19:42 failed under maintenance; a later send failed with `todos.sqlite3 differs from its declaration`. Original sessions/auth/UNKNOWN inputs remain untouched.
