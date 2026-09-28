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
