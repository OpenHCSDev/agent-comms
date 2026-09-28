# Current-incarnation pending/inbox projection

Parent #229 follow-up to #235, based on parent #229, including published integration8004347. Own tree ~/wt/comms-l0a-incarnation-delivery-20260928, branch fix/l0a-incarnation-delivery. No changes to parent history/D22 regions or running installs.

## Code

- bus_route_counts.py: replace hand DDL and raw storage reads/writes with existing A13 TypedTable/TypedRow. Row declarations own timestamp, sender publication lookup, indexed route entries, route totals and source checkpoint. Delete unused scalar target/pair cutoff methods. Preserve durable disposable index and route totals; birth-crossing routes use a covering timestamp index. A bulk exact-seen query does not scan history per thread.
- routing.py: DeliveryMessage uses existing current Message/initial publication decoders. DeliveryScope excludes messages older than current sender/recipient creation, and rejects a mismatched immutable sender lookup from the existing initial sideband. This fixes rename-into-a-removed-name even when both original actors predate their messages. No new message fields or publisher registry.
- MessageBus pending/inbox only: one captured registry/catalog/read ledger, one index sync and bulk query; same filter on direct scan after index outage. Warm calls reuse cached projections. Remove duplicate _pending_route_fields raw validator entirely.
- Direct scaling instrumentation now measures the real Message boundary; no old-validator test adapter. Remove remaining test-only bus.rename_thread call.

## Stores / activation

bus_route_counts.sqlite3 is disposable derived state, rebuilt from canonical bus rows. Its schema is changed and parent must discard this index during quiet cutover; no old-schema reader, upgrader or baseline added. Canonical bus, read-ledger, archived display/history and registry bytes are not rewritten. No live install or restart.

## Evidence

- Existing three named incarnation pending-count tests: PASS unchanged.
- Added current inbox/single/bulk agreement for sender/recipient replacement, retained DM/full history, index outage, unordered timestamps and sparse seen membership: PASS. Covering timestamp index confirmed with actual SQLite query plan.
- Seven focused checks pass in 0.85s.
- Built candidate core wheel and paired Toad107 wheel, installed to Toad owned .artifacts/candidate only. Actual mounted dm_rebind_paint_pilot.py now PASSES unchanged.
- Wider pending/scaling/operations run: 96 pass, two failures remain: old raw-wire mutation fixture expects forged initial sideband accepted; ordinary renamed reader loses old seen membership because ThreadIncarnation.current ignores aliases (ReadLedger dependency, parent-owned). Reporting these separately, not weakening their behavior.

## Pending validation / handoff

Finalize cold/reopened/idle cost evidence and marked guards; confirm two wider failures on parent base. Parent owns ReadLedger/ThreadIncarnation rename-read closure and all live activation. Initial immutable sender proof is reused; non-initial records retain their existing Message timestamp boundary. No assertion changes in the three specified failures or Toad DM rebind.

## ProcessIdentity caller adoption (requested after initial publication)

Integrated S13 #232 dependency 5d2935f in own branch, retaining parent d0380c6. Input-drain merge resolved using S13 Message import and parent's removed cursor import; no hand edits to its implementation.

- ThreadManagement captures actual ProcessIdentity once when numeric OS PID enters claim/attach/rename; persisted constructors and replacements use only process_identity. Active executor replacement compares whole identity, preserving it during metadata updates. Detached fork declarations have no process binding.
- RegistryDocument compares whole ProcessIdentity for owner/admission changes and strips process bindings when restoring durable identities into stopped state.
- Registration live-owner/turn/compaction checks compare the captured calling process identity and expected saved identity, not PID alone.
- Direct registry tests migrated to actual process capture; removed test's mocked os.getpid and empty fixture PID field.
- 24 registry/process tests pass, including real OS identity capture, same-PID different-birth refusal, generation rotation, active-owner protection, saved reload and stopped restoration. Seven pending/rebind checks still pass after S13 integration.
- Actual detached-client new_session now passes former claim_thread TypeError, launches a real worker and reaches prompt. Full prompt/reattach check then times out before its plain backend fixture emits TURN_STARTED; S10 is now integrated. The raw ACP diagnostic identified an unmarked isolated bus, rejected by the current canonical activation boundary. No passing end-to-end prompt claim.

## Final owned closure

- Published core code first at6c1a8ae; parent integrated it atbb1892e. Latest branch merges parent8004347 to test against its actual queue-emitter deletion/S9/S10/S12/S13 implementation. InputDrain conflict resolved by deleting its unused Message import as parent already did; no emitter implementation edits by this owner.
- Final combined registry/current-delivery/real-child-lifecycle/owned-guard run: **33 passed in3.74s** (final-core.txt).
- Cold/reopened/scaling/concurrent-append/current-incarnation selection: **9 passed in4.63s** (performance-current.txt). Reopened unchanged history decodes zero rows; appended history only decodes its increment, registry validation is bounded per snapshot, and writes cannot race index synchronization. These are focused performance invariants, not a workstation-wide CPU measurement.
- Actual installed candidate core+Toad wheels: mounted DM rebind still passes with ProcessIdentity fixtures. Current queue callbacks preserve drafts/UNKNOWN/missing-evidence feedback. Actual in-process ACP queue admission, current emitter, retirement and mounted Toad pass using local transport and an explicitly held backend; no provider-consumption claim.
- Owned runtime delta vs parent8004347: +357/-306; tests +198/-20. Added runtime lines provide the timestamp/source-incarnation indexed projection missing from the replaced raw route counter. Ratchet vs parent: type identity -2, boolean chains -3, string subscripts -4.
- The two broader failures were reproduced on untouched parent364d590. Parent has accepted read-proof rename-alias closure and owns the raw-wire mutation fixture. No suppression, skipped assertions or behavior relaxation here.
- bus_route_counts.sqlite3 remains disposable derived state; parent must reset it at quiet cutover. All live installation and durable D22 rewriting remain parent-owned.

## A1 table membership correction

`RouteTable` scopes the existing `TypedTable.members_with` discovery. Both table creation and whole-projection reset now derive membership from the row declarations; the create tuple and paired reset statements are deleted. No other table catalog exists in bus_route_counts.py. Row-specific queries still reference their owning row declarations. A new-case check declares an additional scoped table and proves initialization/reset pick it up without changing BusRouteCounts. Seven focused incarnation/index/reopen checks pass in3.51s (table-scope.txt). This follow-up is runtime +10/-6, tests +16/-0; no new registry or adapter.
