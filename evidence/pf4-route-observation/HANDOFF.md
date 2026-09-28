# PF4 route observation — urgent live UI blocker

Owner Pascal. Stable core code `cd034e6`, branch `codex/pf4-route-observation-20260928`, tree `/home/ts/wt/comms-pf4-route-observation-20260928`, base parent `67dcb3c` (notification feedback + merged PF1/PF5).
Paired Toad code `04fb611` (PF4 `ff106b8` plus screen lifecycle guard correction), same branch name in `/home/ts/wt/toad-pf4-route-observation-20260928`, base `9523b45` (Toad PR102).

## Actual finding

Parent installed tester PID3722876 held wire/bus locks; parent alone stopped that tester. Worker observed main thread waiting in `locks_lock_inode_wait` before exit. Parent's second `evidence/channel-notifications/installed-ui-live-second.log` shows simultaneous repeated `root_is_current -> current_root -> wire -> Comms -> Registration` decoding/guard hashing on UI and worker threads. Prior UI created a full service even for root equality. Another snapshot puts main thread in `pin_private_nk_launch -> WireLog.locked`. No worker killed user Toad3659247, sent/replayed messages, restarted owners or wrote live state.

This receipt does not claim a proven cyclic PF5 deadlock. Reviewed PF5 callbacks use captured scope, no registry callback under indexed scan. New notification SQL query takes registry snapshot + read-only SQL, not wire/bus lock. Parent's observer read actual rows successfully. Textual captures stdout during run_test; lack of print output alone is not hang evidence. Provider UNKNOWN and final live acceptance remain parent-owned.

## Owner and deletion closure

- Existing `ActiveRoute` now inherits the explicit `CommsRoute` selection contract. `LocalRoute` preserves explicit/environment/unconfigured-default behavior. One `resolve_comms_route` owns selection for both observation and the `wire()` factory.
- `ActiveRoute.observe_root` reuses existing `WireLog._private_marker_unlocked`: trusted ancestry, regular owned mode0600 files, strict marker protocol shape, and exact route root ID. It does NOT instantiate Comms, Registration, read history/ledger, acquire bus mutation lock or run durability scan. No route cache, second JSON decoder or alternative store.
- Actual service construction still calls `bind_owners -> pin_private_nk_launch`, retaining locked durable verification. Default selected writes still use `guard_default_route_write` and sink validation. A route observation is not write authority.
- Toad `current_root` delegates to the route owner, so all existing root-check callers stop constructing services. `app.coordination_wire` validates fresh route/marker each access and only composes when selection changes, discarding a service overtaken by rotation.
- Hidden Comms views return BEFORE route checks. Deleted recurring hidden warmer, cached prepared pages, warm callbacks and background admission machinery; visible reader still owns actual I/O through cancelled waiters. Replaced obsolete warmup test with no-hidden-observation/fresh-resume acceptance.
- Identical notification projection skips repeated layout updates. This is widget presentation equality only, not cached core authority or stopped polling.

## PF2 integration seam (relay to Darwin)

Exactly one new caller: `ActiveRoute.observe_root` accesses `_private_marker_unlocked()['wire_root_id']`. PF2 should migrate it to the typed marker's root identity. No wire_log.py edits here; no duplicate marker validation. Route observation must stay free of bus lock/registry construction. Actual binding keeps existing durable check.

## Candidate paths

- Core: `dist/agent_comms-0.1.0-py3-none-any.whl` (cd034e6).
- Toad: `/home/ts/wt/toad-pf4-route-observation-20260928/dist/batrachian_toad-0.6.20-py3-none-any.whl` (04fb611).

Both handed to parent; deployment, live UI, current user process and serial integration remain parent-owned.

## Focused acceptance

- `core-route.log`: 4 pass — observation while real exclusive bus lock held; constructors/registry decoder/mutation-lock method forbidden; explicit/env/absent default and no directory creation; rotation, invalid ID/schema/modes; real service pin preserved.
- `entrypoint-route.log`: 13 existing private/default-route cases pass, 10 deselected. No providers.
- Paired Toad: 5 channel reader cases pass (revision/page bounds, scope changes, cancellation ownership); channel visibility pilot passes (40 hidden ticks no root checks or query, new data on resume); actual mounted default-route rotation/stale-page/invalid-marker pilot exits0; guarded write admission pilot exits0. Many-tab observation receipt tracked in paired handoff.
- No NRA rerun or CI wait. Original NRA attempt in PR102 used incompatible tool Python for existing Toad syntax; not an acceptance gate.

Core command: `PYTHONPATH=src TMPDIR=$PWD/.artifacts/tests timeout 60 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -o addopts='' -n 0 tests/test_route_observation.py`; existing route selection uses `tests/test_private_nk_entrypoint.py -k 'route or default'` with same options.

## Final receipts

Draft core PR216 https://github.com/OpenHCSDev/agent-comms/pull/216; paired Toad PR103 https://github.com/OpenHCSDev/toad/pull/103.

`actual-route-read.json`: 20 observations of actual default live route with Comms/Registration constructors, RegistryDocument decoder and WireLog.locked patched to fail if called: all20 succeeded; each call count0, no live writes/provider calls. Reported0.004s is descriptive, not a threshold.

Toad many-tab pilot first exposed new PR102 observation timer reading app.screen while stack was empty. `04fb611` uses existing Textual Screen.is_active (already handles empty stack) in observation/notification/history guards. Rerun PASS:10mountedtabs/40observations, hidden sidebars0, return catches up. Production core unchanged. Parent told immediately; candidate wheel rebuilt.

Parent actual installed configured-provider/UI acceptance is still separate; source/local receipts do not claim live send success or explain prior UNKNOWN definitively.
