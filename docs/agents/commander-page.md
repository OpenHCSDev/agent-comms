# Commander page, first slice

Owner: pi-cn-phase6. Core base 011084fc5; Toad base 9f203ea1. Issue #1495.

## Checklist

- [x] Read phase 6, parity and dependencies; phase 5 is not a dependency.
- [x] List all open Core/Toad PR files; read #1912 and #1756 diffs.
- [x] Avoid the recent #1070 MainScreen changes: add a discovered slash command instead.
- [x] Read refactor-audit skill and catalog; run its AST overlay across both production roots (zero parse omissions). NRA is not installed on this machine.
- [ ] Implement observation, board, lazy details and existing delivery alarms.
- [ ] Exercise real scratch Core orders in Toad under isolated Xvfb/st.
- [ ] Run bounded checks in the background; publish paired draft PRs, do not merge.

## Owners and consumers

`TodoStore.for_commander` owns the indexed open-order selection, `Todo.held` owns assignment identity, `Todo.overdue` owns overdue meaning, `ProblemOrder.instructions` owns the full order text, `OrderProgress` owns git/PR state and stall policy, `TurnDigestRead` owns bounded newest-first turns, and delivery progress owns notification labels. The page must render these, not reinterpret strings or keep order state.

`ProgressSource.readings` currently executes the order's acceptance check. A read-only commander page must not execute arbitrary checks. Keep one progress selection algorithm in ProgressSource, with a status-only source capability that does not request distance measurements. The existing native tool continues requesting its existing measured progress.

`ObservationProcess` owns off-UI reads and lifetime; add a declared interest/result rather than a widget polling stores. `WireRevision` must include Todo main/WAL changes and the existing expiry tick. UI expansion belongs to the card; only expanded units request bounded turns.

BOUND-2/BOUND-8: retain Todo, OrderProgress and ThreadIncarnation in typed models, not mapped records or redispatched kind strings. MEMB-1: the new slash command is discovered from its declaration; no command roster or MainScreen change.

## Known gap

Pre-send refusals currently escape as boundary exceptions; no retained indexed refusal feed was found. Existing failed delivery notifications are not the same fact and will be labelled honestly. Requested an owning-source decision from claude-code; continue the independent page work.

## Disposable output

Owned by pi-cn-phase6: `/home/ts/.cache/agent-scratch/pi-cn-phase6` for bounded test logs, one Xvfb/st run and screen captures. Source/handoffs remain in the two `/home/ts/wt/pi-cn-phase6--P*` worktrees. The standard resource-check executable is absent here; measured 3.1 GiB available RAM and 26 GiB disk. Run one UI, no parallel agent or large suite.
