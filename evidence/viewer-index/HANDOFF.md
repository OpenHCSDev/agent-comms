# Reopened viewer index implementation fix

Base main: 4e6e4e5 (PR165). Persistent tree: ~/wt/comms-reopened-viewer-index-20260928.

## Cause and ownership

Reproduced the untouched PR159 failure: reopening a viewer decoded 100 already indexed messages. The display checkpoint itself was reused correctly. The call stack in decode-trace.json shows the extra scan came from MessageBus.pending_counts -> inbox -> _load_log for human readers. Only its process-local count cache avoided this, so every reopened process reconstructed the entire JSONL.

The fix uses the existing BusRouteCounts route totals and the current ReadLedger seen-sequence set. One sequence index supports exact painted-membership subtraction from the existing route totals. No second data store, watermark or read authority is introduced. Holes remain unread; stale DM identity evidence stays invalid. The old unconditional human inbox materialization is deleted. If the disposable SQLite index fails, the existing authoritative bus iterator provides the same exact-membership result.

DeliveryScope remains the routing owner. Its delivers/conversation methods now take sender,target fields, and all current consumers are migrated rather than keeping the old Message-argument signature. Executor indexed counts use those same rules instead of their former parallel inline routing branch.

## Scope boundary

Pascal owns relationship/passive persistence and migration. This fix changes no relationship/passive data or behavior. relationships.py has exactly one mechanical DeliveryScope call-site update; owner_compaction_commit.py has the same one-line update. Remaining changes are bus_route_counts.py, MessageBus/DeliveryScope in declarations.py, and the viewer-index regression. Parent owns combined integration/deployment. No goal or paired Toad code is changed here.

## Verification

- baseline.txt: original failure reproduced (100 vs zero), other two tests passed.
- first-fix-tests.txt: original reopened-viewer, exact read-ledger and scaling tests passed (29).
- regression-tests.txt: 30 passed, including exact sparse human reads, alias/invalidation behavior, zero reopened decodes and SQLite failure fallback.
- verified-tests.txt: 167 passed, 38 skipped across viewer index, read ledger, thread scaling, channel display, declarations, relationships and owner compaction commit. Optional native cases are skipped; no configured provider calls.
- Existing original assertion is unchanged: zero decodes on reopen and at most two for one append. Bus replacement/checkpoint damage and append-race tests still pass.
- NRA full context: exact_compact_global, 79 detectors, none omitted. Existing ThreadStatus external enum recovery and ViewPredicate/SavedView tiny-method duplication remain outside this fix. Authored implementation and local execution evidence, not a native equivalence-proof claim.

No live data, installed package, native bundle, or owner process was modified. Source and evidence stay in persistent storage; task caches are removed after publication.
