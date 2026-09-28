# Native SessionManager entry-store implementation

Branch refactor/native-session-entry-store-20260928 from installed225/combined242/d04105d, persistent ~/wt/comms-native-session-entry-store-20260928. Work in progress; no live changes or provider calls.

## Ownership

Darwin: EntryStore public nominal base, DiskEntryStore (SQLite-derived offsets/selectors, lazy JSONL payloads) and MemoryEntryStore; shared traversal/integrity; SessionManager load/append/CAS/fork/reconcile; prepare/reopen/compaction streaming and all direct consumers. Pascal: AgentSession native proof startup, consuming manager.entryStore.entries()/trackedInputs(). Direct contract posted PR234. Lovelace: >256MiB memory-envelope acceptance. Parent:242 native framing/activation.

## Implemented checkpoint

- Store scans strict UTF8/current-v3 header/ID ancestry/newline incrementally; holds offsets/selectors in disk SQLite, no historical message bodies or whole-file Map. On-demand reads verify descriptor and path revision. Index storage is derived, under persistent owned cache; close/process exit removes it.
- Nominal subclasses share traversal, retained-compaction context, inherited settings, full-history tracked-input/commit search. All history remains in original JSONL; no original data mutation during reads.
- SessionManager replacement patch deletes eager fileEntries/byId and loader/index mechanisms, moves append/fork/reconcile to store consumers. Current patch is still under caller integration; NOT ready for install.
- Store tests2pass include both storage declarations/branches/retained settings/old input+commit lookup/append stale read and strict malformed history. Initial failed log retained: anonymous unlink of live SQLite file refused IO; fixed by actual store-owned index lifecycle. No claim of full memory acceptance yet.

## Required remaining completion (same PR)

Migrate native SDK/AgentSession/RPC/export/interactive direct consumers; complete streamed initial-uncompacted prepare→existing CompactionPolicy chunker (no eager messages/source arrays); prepare/reopen/commit helper current imports and lifetime guards; compiled/package integration; real local native and >256MiB/memory-envelope acceptance. No Proxy, array compatibility facade, new input allocator or replay.

## Continued implementation checkpoint

- PR243 now owns current native SDK/AgentSession/RPC direct consumers; Pascal's proof startup remains separate. Indexed trackedMetadata()/trackedInputMetadata(inputId) returns id/inputId/inputDigest without message body reads and validates observation revision; direct contract sent PR244.
- Initial SDK restore no longer eagerly allocates every historical message. SessionContext Ready/Compaction subclasses own model-policy admission and native input gate; deferred history remains available for get_state count/get_messages/get_entries and owner-selected compaction. Existing CompactionPolicy supplies bytes sizing; no new resource catalog. Protocol history arrays now stream through existing output-guard record queue (no interleaved JSON).
- Existing compact() exercised24 local native stream fixture calls with model-sized inputs, initial-uncompacted lazy message ranges and on-disk intermediate reduction. Source generations reach actual chunker; no fake prepared-input bypass.
- Actual candidate CLI startup/get_state now succeeds on an uncompacted source too large for its model-policy admission; correct messageCount60, no provider call, no source-body preload. First source receipt incorrectly reported0 (caught/fixed by routing count through the context owner); both logs retained.
- Native prepare/reopen/commit validators now consume DiskEntryStore; no separate eager strict parser, no lifetime256MiB file guard there. Commit/reconcile reuses the index observed by its own freshly-opened SessionManager and validates revision while holding original writer lock.
- Remaining: full helper/manual saved-prefix streaming, HTML and branch-summary iterator consumers, final patch/package assembly against Pascal's current startup source, old/current contract fixtures and Lovelace's larger-than256MiB memory/initial compaction acceptance. This is not a finished deployable candidate yet.
