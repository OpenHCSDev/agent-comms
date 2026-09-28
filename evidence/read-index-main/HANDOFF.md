# Standalone main unread-index fix

Backport of246's same owner implementation from1931bad to currentmain51cbfd0.
Branch fix/main-bounded-transcript-read-index-20260928; persistent worktree
/home/ts/wt/comms-main-read-index-20260928.246 remains intact for229 integration.
No237/229 or paired107 prerequisite. Copernicus owns separate mainToad pending
feedback oncc2d35. Parent owns quiet installation.

Only four production files: view_unread.py, read_ledger.py, history_views.py,
presentation.py. Reuses main's already-merged230 TypedTable.select/insert/read;
no modifications to TypedTable or nominal/caller surfaces outside this fix.
Main lacks237's one/upsert helpers, so atomic checkpoint replacement uses the
existing declared select and typed insert after deleting that source's row.
The local _IndexVersion projection types this module's existing PRAGMA result.

Whole-file scan and old raw schemas/mappers are deleted. Existing SQLite index
now records resumable bounded slices and completed state, with fairness and
cooperative record-boundary cancellation. Read cursors survive partial indexing;
only completed exact counts are returned. thread_unread_pending is the same
presentation contract as246. Native IO runs outside display/bus locks. Busy
SQLite is never deleted as corruption. Cancellation does not poison fresh roots.
No input size cap: an oversized complete JSON record may exceed a scheduling
slice and is never skipped/truncated.

QUIET INSTALL: reset ONLY disposable transcript_reply_index.sqlite3 and its
SQLite sidecars for schema3. Do not mutate user/native history, read_ledger.json,
registry, bus markers/floors, coordinator state or other databases. This PR adds
no durable migration/converter. Existing main read-ledger format is preserved.

Evidence:18 focused unread/A13-guard checks pass in2.85s. Actual143,686,055-byte
agent-comms-ux native history returns2486 replies over138 bounded calls: max78ms,
aggregate5.34s, reopen0.49ms. Executor cancellation/join17ms, clean interpreter
exit. Main installed archive UI probe underway using currentmainToadcc2d35 and
Textual16ede fromruntime-watcher-20260928, isolated core wheel installation.
The probe copies current main's actual root to its owned stage (excluding socket
files), and rebinds copied checkpoint/attachment inode certificates only. That
probe-only rebinding is not an activation/cutover requirement. Source untouched.
