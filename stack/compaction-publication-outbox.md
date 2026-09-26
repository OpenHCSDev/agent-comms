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

`pending_publications(session_file)` reads committed pending rows. Local ACP
projection may call `observe_publication(commit_id, metadata_json)` **only after**
a matching local metadata update returns. This marks local delivery attempted;
it is not proof that a remote UI displayed it. If delivery or marking is
uncertain, the same commit-ID-keyed metadata remains pending and may be repeated.
The client must deduplicate by exact ID; this is not a summary broadcast or a
message to a guessed bus recipient. No automated consumer is wired yet, so all
new rows intentionally remain pending and runtime/ACP publication is **OPEN**.

Provider-free tests cover intent/UNKNOWN suppression, native positive commit,
lost result followed by exact-ID reconciliation without resend, process reopen
of a pending row, malformed evidence rollback, metadata mismatch rejection,
repeated exact ACK, second commit with distinct ID, and absence of summary or
recipient fields. Focused owner/journal tests: **74 passed** on pinned disposable
native package (log `/var/tmp/pr95-outbox-owner-tests.log`). The old manager,
installed package, live sessions and provider routes were not modified.

Next integration must bind the publication to an existing local owner ACP session
with an explicit canonical session identity and retain it pending on uncertain
ACP delivery, while blocking fresh sends behind unresolved native commits. This
foundation does not itself activate adaptive compaction or authorize a merge.
