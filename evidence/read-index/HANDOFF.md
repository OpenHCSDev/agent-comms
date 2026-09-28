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

## Initial checkpoint

At the first draft the full UI probe remained; its completed installed result
is recorded below. Parent owns integration and quiet activation; no CI waiting.

## Installed acceptance completed

Isolated installed core wheel + paired Toad107 f902042 + existing parent Textual
render the full retained archive: #comms8, agent-comms-ux2,
pr95-selected-pi-summary-owner0. No messages sent. Watcher and child join;
asyncio executor/interpreter shutdown completes and full probe exits0 under the
original40s external/35s diagnostic limits. See archived-ui.txt.

Source was parent's staged current root:
/home/ts/wt/comms-acp-saved-session-startup-20260928/.artifacts/d22-current-root-latest.
The probe copies it into WATCHER_ACCEPTANCE_STAGE/wire, preserves durable rows,
rebinds copied attachment inode references and current checkpoint certificates,
and verifies existing registry guards. It never writes the original root or
native files. All three views exercise those copied roots. WATCHER_ARCHIVED_WIRE
selects this source; Python3.14 parent runtime-compaction-policy-b3a9c06 supplies
dependencies, PYTHONPATH selects an isolated wheel installation target. Build
with uv pip install --target OWNED_TARGET --no-deps . and the committed paired
Toad107 source; no live package installation. Reproduction script is
archived_ui_probe.py, derived from109's unchanged view/watch/exit assertions.

Retained setup failures are explicit: initial probe used the wrong registry
guard constructor; corrected to current guard verification. Original109/main
Toad imports removed OBSERVATION_INTERVAL and cannot load round2 core. Paired107
fixes those callers and was the installed acceptance target. No compatibility
alias was added to core. Both failed receipts are retained.

Followup verification:12 caller checks pass (oversized2.4MB record counted intact
plus11 read-ledger cases). Four viewer-snapshot tests fail identically on this
branch and untouched13547ee: decoder-count expectation3 vs2, copied bus mode,
old noncanonical input envelope, and renamed human inbox expectation3 vs2.
These are baseline failures, not claimed green; parent receives both logs for
existing surface owners. One additional test passes reopen after cancelling a
shared index owner: a fresh wire does not reuse its cancelled object. Overall
this task has29 passing focused cases plus the four reproduced baseline failures.
Ruff and diff-check pass. No unrelated native/provider/CI checks were repeated.

## Integration receipt

Ready for core integration after required local ratchet.237 remains unchanged.
Native unread work uses bounded record checkpoints by default on viewer_snapshot;
no timeout expansion, background indexing service, parallel store or old scan.
Cancellation is cooperative between complete native records, not a hard bound
on parsing a single arbitrarily large JSON record. Tested actual retained max
batch64ms; valid oversized records are never discarded to meet the budget.

Preserve parent/Nietzsche history_views notification SQL and metadata/floor hunks.
Only viewer_snapshot's native read is moved outside its existing display locks;
its captured immutable snapshot fields are preserved via dataclasses.replace.
One earlier SQL string line was split for lint without changing SQL semantics.
Durable format stays fixed; parent quiet reset only needs the disposable reply
index schema3 file/sidecars for this change. Parent remains activation owner.
Pending-count display adoption was sent directly via CLI comments on109 to
Copernicus; no reply or adoption is verified. It remains a Toad presentation
followthrough, not an unread scan/exit failure. All owned copied roots, wheel
build caches and test artifacts are removed after the processes exit; published
evidence and reproduction scripts remain in this persistent worktree/PR.
