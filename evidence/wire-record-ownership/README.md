# Whole canonical wire-record ownership

Boyle owns this S7/C0 read/record closure from main40b66442 in persistent `~/wt/comms-wire-record-ownership-20260929`. Direct claim posted to Dalton389 before implementation; HistoryViews/public page APIs, ACP live cursor, InputDrain pump and native SourceWitness remain with their current owners. Parent owns merge/live.

## Deleted authority / replacement

- Delete the free `validate_delivery_record` mechanism; existing `CommittedDelivery` owns strict creation/proof, preserving DeliveryPolicy's declaration-derived kind and exact receipt checks.
- Delete WireLog's fused ~95-line private-row decoder and three-item tuple protocol. `WireScan` owns one stream's monotone sequence and duplicate publication-key state; `WireRecord` separates retained/claim messages from existing `CommittedDelivery`.
- Message owns canonical public decode and retained admission. Full, certified, candidate and delivery readers share this boundary; no second codec/row format/store or conversion.
- Checkpoint callbacks receive one verified declaration. Each record owns its existing TypedTable projection; remove nullable receipt/cohort mirrors from callbacks, suffix capture and append.
- Migrate all claim, cohort, response-conversation, publisher, routing, candidate and test/guard callers. Snapshot/paging contracts unchanged for Dalton.

Latest NRA/refactor-audit and S7 reread. BOUND-1/2/3 once boundary decode; IDEN-3 record state; IMPL-4 complete callers; TIME-3 no compatibility; AGENT-6 actual proof ownership. Exact authority, fsync, seals, cross-row duplicates and UNKNOWN retained; no native input replay.

## Evidence / remaining

`record-boundaries.txt`:44 PASS12.10s (one large-volume case deliberately excluded under resource warning): receipt tampering, duplicate key, declared-kind closure, checkpoint crash recovery and changed-prefix/index refusal.
`lint-first.txt`:focused production lint clean.

Claim/candidate focused callers, affected installed native/ACP and ratchets pending. This is a complete source-bearing draft, not yet ready. No live changes or CI wait. Owned .venv/.installed/.scratch disposed after receipt completion.
