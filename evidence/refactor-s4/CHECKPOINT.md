# S4 final checkpoint — 2026-09-27 America/Toronto

Completed S4 and the owner-assigned mounted Toad integration. No pending S4
implementation or mounted validation step; no new overlapping refactor begun.

Core worktree /home/ts/wt/comms-refactor-s4-read-routing-20260927;
branch codex/refactor-s4-read-routing-20260927.
Draft https://github.com/OpenHCSDev/agent-comms/pull/137.
Core source: a410de9fcc13407ea4c3bbfdca9c47ad87c7f26c, confirmed remote/PR head.
Includes full implementation 4aa1371, normal main merges f75dc81 and b3acdf2.
PR136/main b0d4900 restore_stopped/restore_missing additions are preserved.
This final checkpoint commit changes evidence only, not validated source.

Toad own worktree /home/ts/wt/toad-s4-read-routing-20260927;
branch codex/s4-mounted-read-basis-20260927.
Draft https://github.com/OpenHCSDev/toad/pull/77.
Toad source: e9a0542486b946a0ab24f645751266eb3693dd48, confirmed remote head.
Base 0894005, implementation b71aa26, final mount-publication ordering e9a0542.
Final evidence-only publication commits may follow these exact source revisions.

Recovery/process evidence: owner confirmed prior CLI absent after control-group
termination. On this invocation Git/remote inspection found local b3acdf2, remote
40c5599 and no PR: prior push had succeeded, prior PR creation had not happened.
No uncertain Git mutation was replayed. Current CLI observed PID 1910606 under
/user.slice/user-1000.slice/user@1000.service/app.slice/
comms-independent-s4-20260927.service; it is independently owned. Previous source
commits, WIP and .artifacts/s4 logs/caches retained. No new worker/model override.

Validation: previous full-surface shards retained in HANDOFF.md. Latest core
54 passed (4.33s), following partial-paint 40 passed and PR136 integration 25
passed. Eight mounted Toad pilots plus four reader cases pass. New bounded-page
and bus-replacement cases fail on archived original Toad. Full core NRA scan
complete 79/79 detectors, 23 findings (baseline 28); focused Toad report with
whole Toad/core context complete 79/79, one existing HistoryKind finding. No
source equivalence/native proof claim. Core Ruff, DisplayBasis mypy, Toad scoped
Ruff and diff checks pass. No paid providers, CI wait, installation or deployment.

Remaining integration belongs to parent: merge foundation/A8 PR129 once, merge
S4/other disjoint surfaces normally, merge Toad PR77 together with core PR137,
refresh coupled core/Toad pins, and decide live activation. Native runtime/PR95,
coordination_store/coordinated_runtime, restoration and recovery files, Textual,
stack pins and shared live roots were not edited by S4. Three narrow preexisting
alias/role consumer follow-ups are explicitly handed off in DISPATCH.md under
their owners; their current values/behavior are unchanged.
