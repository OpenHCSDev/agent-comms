# PR95 keyed compaction publication outbox — durable foundation, not live delivery

`CompactionJournal.resolve(..., publication=True)` now inserts a **metadata-only**
row in the same SQLite DELETE/EXTRA transaction as a verified committed native
outcome. Only the trusted owner bridge requests this for a child-returned or
exact-ID-reconciled `committed` outcome. The record is keyed by the exact native
commit ID and contains only `{commitId,entryId,revision,leafId}`. No summary,
prompt, model text, recipient, or bus envelope is stored. An `intent`, `unknown`,
or `aborted-no-write` cannot create a publication. Failed validation rolls back
both outcome and publication; post-COMMIT directory-fsync uncertainty remains
UNKNOWN and cannot authorize a new native dispatch.

`pending_publications(session_file)` reads at most 32 committed pending rows,
revalidating their exact metadata against the durable native evidence. The ACP
owner turn now projects them to its existing local session **before any next
provider input send**, only if owner PID, canonical thread, session file and an
attached transport agree. If no listener exists, nothing is ACKed. After a
matching local metadata update returns, `observe_publication` marks local
delivery attempted; it is not proof that a remote UI displayed it. If local
transport delivery fails before marking, the exact commit-ID row remains
pending and may be projected again. If `observe_publication` raises on
post-COMMIT parent-directory fsync, its committed update may already be
**observed** despite the exception; a fresh exact-ID database read may instead
find it pending. Do not assume rollback, undo, or native retry from this error:
reconcile the exact row state. Reprojection is allowed only for a pending row,
and the client must deduplicate by exact ID. This is not a summary broadcast or
a message to a guessed bus recipient. It never publishes an `intent`, `unknown`
or malformed/tampered row.

Provider-free tests cover intent/UNKNOWN suppression, native positive commit,
lost result followed by exact-ID reconciliation without resend, process reopen
of a pending row, malformed evidence rollback, metadata mismatch rejection,
repeated exact ACK, second commit with distinct ID, and absence of summary or
recipient fields. Focused owner/journal tests: **74 passed** on pinned disposable
native package (log `/var/tmp/pr95-outbox-owner-tests.log`). The old manager,
installed package, live sessions and provider routes were not modified.

The subsequent ACP send-admission slice checks that saved session's journal
inside the existing wire-lock-protected final send boundary, before input bind,
native-start credit or goal-wait consumption. Missing journal before any commit
is normal; malformed/unreadable journal or unresolved intent/UNKNOWN refuses.
This covers original ACP input and the same boundary used by queued/steered
corrections; no unknown input is replayed automatically. Provider-free ACP,
channel, goal/correction, journal and real-native tests: **66 passed**
(`/var/tmp/pr95-send-admission-focused.log`). A separate dangling-journal-link
negative refuses without repair. The idle manager still needs explicit disposal
and validated fresh reopen; no provider-free test may silently 'recover' a
failed native manager instance.

The local ACP projection is now wired; provider-free tests prove no-listener
retention, exact ID/non-summary output, failure-before-ACK then exact duplicate
reprojection, tampered evidence refusal, and projection before the next ACP
send. The current send gate blocks unresolved native work. What remains OPEN is
an owner-only adaptive summary preparation/call site that discards the idle
manager before external native mutation, plus multi-round E2E and operator
recovery. This incomplete runtime slice does not activate adaptive compaction
or authorize a merge.
