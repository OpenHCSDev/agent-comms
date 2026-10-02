# Async bus read and publication custody

Owner: Arendt. Separate continuation of the useful #508 sequence checkpoint.
The matching installed #508 channel-click journey passes; this draft does not
claim the remaining cross-owner bus concurrency is fixed.

## Existing owner search and required relation

The physical owner is `StoreLock`, acquired by `_store_lock`; bounded physical
wait is already owned by `StoreLockContention`. `WireLog` owns ordered append,
marker reservation and certification. `CertifiedSourceRead` borrows its opened
file and SQLite connection from that one barrier. `PrefixWitness` describes the
original sealed prefix; `FinalSeal`/`PendingSeal` own current/recovery relations.
`DeliverySources` owns exact original pointers and validates frozen rows, not a
second message store. None of these facts needs a replacement family or cache.

Two existing read lifetimes differ. `_record_snapshot` opens a fixed inode and
byte boundary and releases the physical writer lock before public decoding.
Conversely `conversation_sources`, reference windows, addressed pages and exact
cohort resolution decode original typed payloads inside the certified lock.
`certified_read` cannot simply become a shared lock: its acquisition guard can
complete/recover an original writer checkpoint. Publication retains the original
wire -> bus -> registry -> coordinator order; no provider await belongs there.

The source census includes history/sidebar/notification, transcript routing,
source coverage, goal waits, cohort foreground, original input and response
publication consumers. Current-fence consumers at actual admission/publication
remain distinct from display consumers of an immutable bounded original cut.
Read snapshots grant no input, cursor, replay or owner authority.

## Implementation trajectory

Extend the original resource owners so bounded original bytes/pointers are
captured once under certification, and display decoding happens after release.
Keep exact root/seq/id/audience checks with `DeliverySources`; delete replaced
locked decode paths across every affected reader. Current proof consumers retain
freshness at their actual write boundary. Reuse `StoreLock` acquisition/custody
for async wait; do not block owner loops with flock/sleep, move coordinator
connections across threads, or introduce a second lock/admission algorithm.

Mendel owns the disjoint Toad caller census and migrations only if the original
Core API needs an explicit change. Existing readers already run in worker
threads; no speculative frontend wrapper, snapshot cache or high-water copy is
required. Singer owns NativeSourceCursor producer/consumer closure separately;
Einstein owns compaction preparation hooks.

Order: semantic source/caller closure, coherent implementation and deletion,
then one batched affected sanity and actual configured multi-owner/publication
and isolated UI read qualification. Original UNKNOWN/native inputs and durable
bus/session bytes are protected. No public prompt/replay/restart is authorized to
this worker. Parent owns release publication.
