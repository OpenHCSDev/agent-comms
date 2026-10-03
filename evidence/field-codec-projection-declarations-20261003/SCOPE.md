# FieldCodec projection declarations

The existing sealed FieldCodec owns field/type/projection discovery. Its immutable class declarations are memoized for fields and type hints, but every encode/project/decode rescanned the MRO for projected descriptors. All three consumers only read the descriptor mapping and invoke the original getter on the current record. Their values remain derived; no row, registry, context or revision is cached.

Reuse the existing bounded declaration memoization in FieldCodec. One declared capacity256 replaces the two literal capacities and supplies all three lookup decorators. The projection result is read-only through existing types.MappingProxyType. No new class, registry, scheduler, state store or consumer cache. New class identities and view names remain distinct cache keys; eviction repeats declaration derivation, not semantic work.

Before evidence uses existing refactor-audit Package.load: Core311 production/362 tests/53 tools, Toad289 production, Textual249 production, zero parse omissions. One projection-discovery declaration and three codec consumers; the similarly named Toad PermissionController._projections is a different owned resource, unchanged. Declared @projected properties are enumerated. Dynamic setattr/delattr candidates were read: instance state, native reactive/style machinery and method annotations, not mutation of these projection declarations. This is source evidence, not a proof against arbitrary runtime monkeypatching. Existing _fields already assumes immutable declarations in this process.

No dispatch or schema behavior changes. Cached descriptors preserve the same reversed-MRO selection; each consumer still invokes getattr(value,name) on its current value. No copied family roster or alternate codec. Pattern IMPL-13 is a comparison point for repeated derivation work at the existing owner, not a claim of competing semantic authority.

## Final verification after implementation

Initial test command refused before collection because system pytest lacks the xdist plugin requested by repository addopts. Same bounded batch with addopts empty:38 passed,1 failed,78 deselected in0.54s. The failure is the unchanged test_coordination execution fixture supplying removed exact_target to ExecutionRecord before codec use. Raw failure is reported; no test or unrelated product workaround was added and no broad rerun was made.

Changed-source read of the actual live coordination.sqlite3 used original CoordinationStore.observing (mode=ro/query_only), original typed row owners and current getter comparisons. ExecutionRecord, AttemptRecord and WakeAssignment each supplied50 actual rows with50 distinct projections. All original projected getter values matched; declaration cache447hits/15misses. No public write/provider/input. This confirms actual stored-data projection under changed source, NOT installed runtime/UI readiness or CPU/latency improvement.

The installed changed-code read remains assigned to the normal receiving integration owner on an existing isolated holder before scoped Ready. Current default485 and rollback334 are unchanged. This is not a provider or whole rendering performance claim.
