# Lock-held decode checkpoint 1 evidence

Base: `d4aaf91395e32c9817dada4e5525553f1ef8e468`

## Owner and consumers

Before: `BusAppendIndex` owned only JSON checkpoint persistence. `BusRouteCounts.sync` separately owned source seeking, line decoding, tail hashing, schema setup, checkpoint comparison, and a lock spanning replay plus commit.

After: `BusAppendIndex.capture_segment` owns bounded source seek/read/JSON decode/tail capture. `ProjectionCurrent` and `ProjectionBehind(through, size)` own coverage behavior. `JsonCheckpointProjection` and `SQLiteProjection` are capabilities composed with that base. `SQLiteProjection.sqlite_reader` owns the read-only `mode=ro`, `timeout=0`, `query_only=ON` WAL snapshot. `BusRouteCounts` owns only route replay and transactional route-row commit.

Migrated production consumers:

- `MessageBus._from_route_index`: committed read only; no `sync` or rebuild. A valid behind checkpoint combines committed rows with only the uncovered source suffix for pending counts.
- `candidate_maintenance._drain_candidate`: append-owner worker advances route counts and checkpoints WAL after catch-up.
- `BusActivityIndex` and `BusDisplayIndex`: existing JSON checkpoint behavior now comes from the composed JSON capability.

Deleted route-local source mechanisms: `_tail`, route-local `seek`, route-local `readline`, context-managed read/write connection mode, and decode inside `_store_lock` / `BEGIN IMMEDIATE`.

## Structural searches

The approved pre-edit family map is `/home/ts/.cache/agent-scratch/slop-census/map-lock-held-decode.md`: 8 true sites, 2 false positives; symbol 138 hits, overlay 105 findings, NRA 28 findings with all 81 detectors complete, call graph 64 hits, twin search 64 hits.

Post-edit symbol/call search covers every `BusRouteCounts`, `sync`, route checkpoint, and projection-state consumer in `src/agent_comms` and `tests`; receipt: `/home/ts/.cache/agent-scratch/slop-census-route-bench/consumers.txt`. Scoped post-edit NRA analyzed 81/81 detectors with zero omissions. Its one finding is the pre-existing declaration-owned `RouteEntryRow.indexes` field list, not a new dispatch/state owner. Receipt: `/home/ts/.cache/agent-scratch/slop-census-route-bench/post-nra-full.json`.

Parse omissions: zero. Unresolved dynamic calls: the externally supplied `decode(record)` boundary and read-only `query(connection)` projection callback. Both are explicit typed boundaries; neither selects state or lock policy.

## Verification

- Focused two-worker suite: 50 passed in 11.09 s.
- Source-selected installed `agent-comms` entry point over an isolated private root: `threads` returned recipient pending=1 and sender pending=0.
- Synthetic sparse 1 GiB source while replay was blocked outside projection custody, 40 reader samples: p95 0.35 ms, max 0.66 ms.
- Commit lock, 41 samples: inclusive p99 1.99 ms, max 3.10 ms.
- One-row sync, 40 samples: p95 1.35 ms, max 2.42 ms.
- Benchmark receipt: `/home/ts/.cache/agent-scratch/slop-census-route-bench/results.json`. The source had a 1 GiB logical size and 4.0 MiB physical allocation; only the bounded first segment was valid JSON and replayed.

## Line count

`bus_projection.py` + `bus_route_counts.py`: 402 -> 640 lines (+59.2%). The route file deletes 153 old lines, including the duplicated lock/decode lifecycle, but the checkpoint does not meet the requested +10% combined-file target. This remains a delivery blocker unless later migrated sites amortize the shared capability or the base is reduced before review.
