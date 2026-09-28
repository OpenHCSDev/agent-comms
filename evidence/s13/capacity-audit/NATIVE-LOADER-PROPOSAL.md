# Native session loading: proposed replacement in the existing owner

Status: concrete implementation proposal, not implemented or accepted live. The143MB retained-session manualACP/commit/reopen acceptance is already passing per parent; this closes future history growth rather than redoing that proof.

## Owner and external format

Keep native **SessionManager** as the sole session/tree/append/revision owner. Preserve the existing complete append-only v3 JSONL and Pi/ACP formats. Replace its eager fileEntries/byId object retention in place. The session file remains authoritative; do not add another session registry or truncate summarized history.

Within this owner, introduce one public nominal entry-store abstraction with concrete persisted/indexed and transient/in-memory implementations. Shared tree traversal, retained-context selection and integrity behavior belong on the base; subclasses implement reading/storing entries. No switches on a storage-kind string, wrapper pretending to be Array, proxy, or alternate compatibility loader. Migrate consumers to the declared contract.

The disk index is a **derived view**, not another durable authority. It records ID/parent/type, byte offset/length and the minimal declaration-owned selectors needed for compaction, labels, model settings, tracked-input IDs and commit identities. It must not contain duplicate message bodies or a second write ledger. Bind it to the open file's device/inode/revision and validated extent. Rebuild on an unrecognized revision; extend only under the existing writer/CAS rules. An invalid/missing index never authorizes a replay or discarding history.

## Incremental reader and resource ownership

1. Scan the actual file incrementally from an open descriptor. Fatal UTF-8, JSON shape, strict v3 header, duplicate ID, parent-before-child ancestry and complete newline tail remain enforced. Compare descriptor and path revisions around the observation; never bless a stale parse with a later stat. Scanning must not repair or rewrite a file.
2. Keep read buffers bounded by the native session resource owner. Decode/release each record, retaining offsets and required selectors. For records beyond available decode memory, incremental JSON token parsing must extract selectors or produce an explicit resource-admission failure before allocating the whole record; silently skipping records is forbidden. A fixed lifetime file-size guard is not admission control.
3. The index itself must be paged/spillable under that same resource owner; replacing all objects with an unbounded Map only moves the growth wall. Use owner-controlled persistent storage, never /tmp or /dev/shm. Existing environment has Node26.5.0 and node:sqlite DatabaseSync, making a local paged derived index feasible without a provider or package install; this is environment verification, not a committed cross-platform dependency choice. Account for index disk and cache memory, close owned descriptors, and clean rebuildable artifacts when no process owns them.
4. Decode requested payloads on demand. Stream history export and tracked-input scanning. Hold only the admitted active context/preparation and a resource-accounted payload cache. Keep IO/progress/cancellation with the transport/child owner; no new whole-job elapsed limit is introduced.

## Retained branch without deleting history

Walk ancestry by indexed metadata, select the newest compaction on the selected leaf's path, and load its summary, firstKeptEntryId-through-compaction retained entries and later descendants. Preserve model/thinking settings folded from earlier ancestors without materializing their message bodies. Follow branch IDs rather than assuming the last physical compaction applies to every branch.

The compaction preparation view must preserve chronological position and parent/ID references expected by prepareCompaction; do not pass a reordered buildContextEntries array as a substitute for getBranch. Historical lookup/export/fork retains access to every entry through the same store. A fork streams the requested history to the existing new-session writer; it never truncates the source or discards an alternate branch.

For initial uncompacted histories, there is no prior summary to discard. The same owner must stream source into the existing CompactionPolicy chunk plan, rather than build a full messagesToSummarize array. This is necessary for a systemic guarantee; optimizing only already-compacted sessions leaves the first-compaction OOM path intact.

## Required caller/deletion closure

- Native session-manager loadEntriesFromFile/acReadStrictSession, _loadEntries/_buildIndex, fileEntries/byId ownership, branch/context selection, append/rewrite/fork and commit reconciliation adopt the store. Delete eager all-history loaders rather than retain both.
- Prepare helper consumes a read-only store snapshot with observed revision, not SessionManager.inMemory(rows) carrying all historical objects. No writer can run during preflight.
- Reopen helper validates strict history and returns identity from the same reader owner without materializing all payloads.
- Commit child validates its saved session through that owner, then uses existing inherited-FD authority, session writer lock and CAS. Delete the separate whole-file JSON parse before SessionManager.open. Stream/search commit identities across the complete history; a commit older than retained context must still prevent replay.
- Parent Python prepare/reopen/commit source checks delegate native semantic validation and preserve file identity/ownership/CAS. Retire every256MiB lifetime guard together with the eager allocation it protected, including pr48DiskRevision. Do not merely remove guards first.
- Native sdk, AgentSession startup and compaction paths consume iterators/retained projections. getEntries and getBranch callers must not force all-message arrays during ordinary startup, preparation or append. Explicit all-history exports must stream to their sink.
- S10 _loadNativeInputState uses the same validated store view and incrementally reads its proof journal. Remove the16→128MiB headroom patch once this owner path is replaced. Preserve all historical claims/digests and UNKNOWN/no-replay semantics; no ID eviction to stay under1024 or any later ceiling.
- Darwin framing/progress remains separate: transport decode checks must agree with admitted actual native output, without converting old file counts into response limits.
- Native patch ordering, generated artifacts, import fence and complete-tree manifest are regenerated once by parent integration. Parent metadata patch stays parent-owned.

## Acceptance that distinguishes this from another cap removal

Use a generated strict session larger than256MiB on owned disk, with many small records and a small retained context. Run native strict-open/preparation/commit/reopen in a subprocess under an explicit memory envelope smaller than the file. Measure peak RSS; assert no provider network. Repeat with larger historical bodies/alternate branches and verify working memory follows active payload/cache and index budget, not file bytes. Clean generated files after child retirement.

Also cover: malformed UTF-8, incomplete tail, duplicate/forward-parent IDs, mutation/replacement during scan, multiple compactions on different branches, model/thinking settings inherited before retained floor, metadata paths retained across compactions, old tracked inputs/commit IDs outside retained context, current uncommitted/UNKNOWN commit reconciliation, and fork/export preserving historical content. A separate initial-uncompacted large source must reach the real chunker without whole-array growth. Cancellation must reap the real child; no replay/automatic resend.

## Why this is not an independent S13-only patch

The native manager and compaction consumers are active S9 ownership; startup proof consumers and tracked-input authority are S10 ownership; the generated manager is also the artifact touched by parent's metadata patch. A12 already accepts the caller's deadline and supplies correct child retirement. The meaningful change spans those owners, not child_process.py. Implementing only a new scanner/index in S13 would leave it unused or maintain duplicate mechanisms. This audit therefore supplies the exact joined replacement rather than publishing a dormant foundation or weakening existing memory guards.
