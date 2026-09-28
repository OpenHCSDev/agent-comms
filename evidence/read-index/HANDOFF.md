# Bounded native transcript unread indexing

Independent followthrough to Toad109, based on ready S12 head13547ee / PR237.
No live installation, providers or source-history writes.

## Existing owner and deletion

TranscriptReadState retains the sole transcript_reply_index.sqlite3 projection.
Its former whole-file loop is deleted. Each refresh commits an atomic resumable
batch with a150ms/1MiB/512-record shared budget and256-record source quantum.
These are scheduling slices, not input limits: a complete record is indivisible
and may exceed the slice; it is never skipped or truncated. Rotation prevents
large early sessions starving later ones. The existing1s UI revision drives
further work; no extra worker, scheduler, parser or parallel index is introduced.

close() signals cancellation before taking the index lock. Native reads occur
outside the display/bus snapshot locks. SQLite lock wait is50ms and busy errors
are never treated as corruption/unlinked. ReadLedger is read once per batch;
its durable cursors are not invalidated by a partially indexed prefix.

counts(viewer,sources) now returns TranscriptUnread(counts,pending).
CoordinationSnapshot.thread_unread holds only exact completed counts;
thread_unread_pending holds incomplete names for UI indexing feedback. Copernicus
has this API through Toad109's comments. A pending count must not be presented as
an exact0. Existing Toad numeric rendering still needs his feedback adoption.

## Files and cutover

Core: view_unread.py, read_ledger.py, history_views.py, presentation.py.
Tests: test_thread_unread.py; evidence/read-index/retained_probe.py.
No changes to237, watcher lifecycle, notification SQL semantics, bus floors,
metadata, provider admission or current native proof.

Runtime/disposable: transcript_reply_index.sqlite3 schema3 adds complete to the
existing ReplyIndex row. Parent must reset that derived file and SQLite sidecars
under quiet cutover; no runtime old-schema reader or migration is added.
Durable: read_ledger.json remains unchanged in format and data; native session
files are read-only. Existing corruption rebuild stays restricted to corruption.

## Verified

-16 focused SQLite/unread/architecture cases pass in3.02s: real incremental
  resume, replacement, writer-tail, cursor preservation, fairness, cancellation,
  busy-database preservation, package typed-table guard.
- Actual retained agent-comms-ux143,686,055-byte native history:138 calls,
  maximum64ms,4.98s aggregate,2486 replies, completed reopen0.49ms.
- Actual retained-history executor cancellation+join17ms; asyncio.run returns
  and interpreter exits0. Evidence retained.txt includes final completion marker.

## Remaining acceptance

Full copied archived-history Toad three-view probe and installed own-candidate
acceptance remain.109's exact WATCHER_ARCHIVED_WIRE path requested directly from
Copernicus. Parent owns integration and quiet activation; no CI waiting.
