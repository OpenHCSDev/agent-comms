# Current-incarnation pending/inbox projection

Parent #229 follow-up to #235, based on integrated parent364d590. Own tree ~/wt/comms-l0a-incarnation-delivery-20260928, branch fix/l0a-incarnation-delivery. No changes to parent history/D22 regions or running installs.

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
- Actual detached-client new_session now passes former claim_thread TypeError, launches a real worker and reaches prompt. Full prompt/reattach check then times out before its plain backend fixture emits TURN_STARTED; S10 dependency not yet integrated here. No passing end-to-end prompt claim.
