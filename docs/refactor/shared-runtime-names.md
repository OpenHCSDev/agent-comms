# Shared runtime names: P62/P67/P68

## Session-load family checkpoint

The existing session-load family owns both names and ACP metadata. Its five
Python declarations and the lifecycle consumer now use `SessionLoadPlan`.
The declaration affix changed with the declarations, so the saved `kind` values
remain `ensuring`, `existing` and `failed`. Both ACP metadata keys are unchanged.
No compatibility names or alternate decoder were introduced.

Source evidence: NRA indexed all 780 Core production/test/tool modules with zero
parse omissions. The related paired-Toad/dependency census indexed 1,387 modules,
also with zero omissions. Source sites were read before the exact AST migration;
arbitrary dynamic attribute construction is not resolved by that inventory.
The migration also enumerated route-scope sites; those are a separate unfinished
working-tree change, not part of this checkpoint.

Two targeted checks pass for default, witnessed and failed metadata encoding and
decoding. These are source checks, not installed ACP/reconnect acceptance.
Forms owns the paired Toad session-load consumers and reconnect repair. That
paired change must be integrated before a deployment with the renamed Core API.

## Still unfinished

The cursor family must become distinct native/wire states while preserving the
saved `receipts` key. Its codec capability and migration are not accepted here.
Native settings P62 remains in its separate worktree; it is not an installable
part of this checkpoint. No fork, TRIAGE-to-FULL or reconnect acceptance is
claimed by these checks.
