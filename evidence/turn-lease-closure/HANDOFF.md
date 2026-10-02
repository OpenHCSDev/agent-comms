# Turn-lease deletion closure — Pascal

Persistent tree `/home/ts/wt/comms-refactor-turn-lease-closure-20260928`; branch `codex/refactor-turn-lease-closure-20260928`. Source commit `b2814a1567c9b63b0b63e71caa0d1e1c73fe841f`, cleanly rebased onto main173 `f5ddd74` (includes172 and Darwin173). Parent handles integration/deployment. No live root/provider/UI changes or extra agents.

## Concrete completed surface

`AUDIT.md` ties this leftover to S5 identity ownership and S7 Registration/no-forwarder requirements; it also records current C0 coverage for parent's next-scope planning.

- Deleted `Registration.finish_claimed_turn`, `Registration.finish_claimed_turn_with_fence`, `RegistryDocument.finish_claimed_turn_with_fence`; deleted their ID-only branch and its compatibility-only test.
- `Comms.finish_turn(lease: TurnLeaseFence)` replaces `(name, turn_id, expected=None)`. No compatibility overload/alias. Registration and document expose one `release_turn(lease)` operation, with the document owning mutation and Registration owning its lock/commit.
- `Thread.turn_lease` derives a witness from that thread snapshot's existing identity/admission fields; no new stored state, scalar adapter or counter. Unattested historical turns remain readable data, with no lease.
- Comms begin-turn rollback and coordinated runtime cleanup retain the lease captured at admission, so delayed cleanup cannot release a replacement using the same textual ID. TurnRunner's sole call changes to pass its already-owned lease.
- Fixed the related Comms.register defect: re-declaring an active executor's metadata now preserves owner/admission counters and the live lease; actual owner replacement remains forbidden during its active turn. Inactive new-owner admission still rotates counters.
- All src/test consumers migrated. No current Toad source calls these APIs, so no additional Toad patch is needed. Exact changed paths are in `changed-files.txt`: six production files plus focused consumer/race tests.

## Acceptance and limits

All tests use existing integration venv, absolute `PYTHONPATH=/home/ts/wt/comms-refactor-turn-lease-closure-20260928/src`, `-o addopts=''`, owned basetemp and60s shell bounds.

- `core.txt.gz`:192 passed,1 skipped,1 failure. Includes registry persistence/identity/guard, active turn, goal standby/liveness and maintenance. Failure exposed the active-registration admission defect above.
- `fix.txt.gz`:4 passed,1 fixture failure. The original active-turn test (now also asserting both counters and lease preserved) and rollback/rebind/revocation race cases passed. New historical-data fixture accidentally supplied pre-JSON tuples to the strict decoder; corrected to an actual JSON roundtrip.
- `data.txt`: that exact data fixture passed. Current histories retain unattested turns without granting a lease.
- `consumers.txt.gz`: timed out at60s after293 success dots, no reported test failure; **not a completed green suite**. Collection lists301 cases across ACP/private delivery, coordinated runtime, operations/start/restart and TurnRunner.
- `remaining.txt`:9 passed (all8 TurnRunner cases plus actual local restart). This resolves the unfinished tail without repeating the earlier broad functional batch. Actual local worker/socket restart, cancellation isolation, shutdown, exception provenance and original-send refusal passed.
- Existing renamed/reused-ID/old-callback/goal-wait tests migrated and passed in core. New rollback-after-replacement, delete/rebind-with-same-ID, revoked-admission cleanup and historical-data tests passed as detailed above.
- Ruff I/F and `git diff --check` passed. Main173 rebase was clean and touched unrelated SavedView/ViewPredicate definitions; no extra optional functional re-run.
- `nra-current.json`: exact_compact_global,79 analyzed,0 omitted,complete,0 findings. `nra-command.sh` is the exact executed command (original status/registry/operations targets, entire src package context including changed runtime consumers). Count-only payload interpreted through finding_count. Structural analysis is not native equivalence proof.

No CI gate, configured-provider proof or installed acceptance claimed. Failed/partial evidence retained and compressed. Owned tests/scanner exited before cleaning24MB fixtures and local pytest/bytecode caches; no worktree or live data removed.

## Remaining work

Parent integrates this small completed API migration; current Toad has no affected callers. Next explicitly assigned scope is remaining C0 operations ownership extraction, with parent owning its planning. `AUDIT.md` and `c0-current-inventory.json` quantify the incomplete original carve; there is no claim that all broad S7 decomposition was already finished.
