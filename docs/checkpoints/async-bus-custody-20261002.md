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

## Working code checkpoint — bounded read capture

`CertifiedSourceRead.capture_deliveries` captures original sealed pointers and
bounded raw row bytes while the certificate is held. Its returned decoder owns
only those immutable bytes/pointers and the original root ID: no live stream,
SQL connection, current marker, owner status or admission authority survives.
`DeliverySources.read_bytes`/`decode_bytes` retain the single original seq/id and
frozen sender/audience validation algorithm; the ordinary locked delivery path
uses these same methods. No new class, store or wire format was introduced.

`WireLog.conversation_sources` and `deliveries_for_references` now finish
certification and release their publication lock before consuming the captured
decoder. `SourceCoverage._page` captures its original page witness, addressed
high-water and marker floor under the same certificate; it decodes after leaving
the bus lock. Whole-prefix/UNKNOWN and final cursor publication checks remain
with their existing owners. `TranscriptRoutes` and `TaskSources` consume the
same API *inside their actual write custody*, preserving original publication
freshness. Deleted free algorithms `delivery_references_unlocked` and
`conversation_sources_unlocked`, including every production/test import.

This is a published implementation checkpoint, not Ready. Exact remaining
relations: physical acquisition still needs the shared async wait algorithm;
`full_history`/`total_messages` and context-manifest reads still retain a writer
lock while decoding; goal-wait addressed iteration needs bounded snapshot
iteration; exact keyed response lookup still resolves under its publication
transaction. Fresh source/prewrite and publication/cursor transactions must
consume the original current fence, not a display iterator. Source/admission
scheduler callsites must use async acquisition without moving live coordinator
connections across threads. These are owned remaining closure, not hidden
compatibility fallbacks. No validation or provider journey has been claimed for
this checkpoint; validation follows the coherent source implementation.

## Working code checkpoint — physical async acquisition

The existing `Platform` family now owns its irreducible native file-lock attempt
and release: inherited POSIX last-close custody and original Windows byte unlock.
`StoreLockContention.waits` is the one bounded physical wait algorithm. Its
synchronous driver sleeps; its async driver yields to the owner event loop.
Untimed synchronous POSIX callers keep their original kernel-blocking acquisition,
so this change does not introduce polling into synchronous legacy callsites.
No new OS family, state store, timeout, provider retry or lock registry exists.

`_store_lock` and `_async_store_lock` share `_store_lock_file` descriptor custody
and `_held_store_source` durability/refusal/release behavior. Cancellation while
an async acquisition waits closes the unacquired descriptor; it cannot leave a
background lock-acquisition thread or a later stray write. Acquired guard/consumer
failure closes the same original resource. TrackedTurnSession.send now awaits
maintenance-wire acquisition before the original capability write. Its final
pipe-drain watchdog and irreversible prompt-writer boundary remain unchanged.

This does not yet claim all async owner callsites are migrated. Certification
itself can recover a damaged prefix and still runs synchronously inside custody;
that operation must remain one guarded owner transaction, with async callsites
borrowing capture work through a complete owned operation rather than exporting
its SQLite connection to a thread. Remaining reader/publication relations above
are still explicit closure obligations. No tests or installed journey have run
for this unfinished source checkpoint.
