## Whole journal transaction ownership (Q8/Q13 continuation)

Deletes the former CompactionJournal operation/enrollment/selected/publication API and migrates its production and test callers directly to operations, summaries, private_inputs and publications. The root retains the actual SQLite lifetime, derived schema installation and COMMIT+directory-fsync policy. No compatibility entrypoints or second connection policy.

The ownership change also removes repeated mechanics:
- SessionJournalHistory capability derives the fresh-enrollment exclusion set from canonical TypedTable declarations; no hand-maintained three-table roster.
- UnresolvedJournalHistory derives membership from each table's declared state field; one unresolved query implementation replaces five copies.
- SelectedSummaryAttempt owns blocking interpretation and exact-record lifecycle CAS, used by every selected transition. Receipt minting stays exclusively after returned terminal COMMIT+fsync.
- PrivateInputs owns the single raw-input exclusion check for both durable prewrite reservation and the lock held through the pipe write.
- Native outcome and publication insertion remain atomic in the same existing transaction; publication observation remains exact metadata acknowledgement.

Latest NRA/refactor-audit reread; patterns IMPL-12, MEMB-2, IDEN-1, TIME-3, AGENT-6. Plain source moves are not counted as semantic factoring. SQL schema and durable record shapes remain unchanged. No live or installed-default changes; parent owns integration.

## Verification in progress

Noneditable isolated install. Focused journal/fresh/summary tests passed 90 cases with two optional skips after caller migration; a stale failure-message assertion is corrected. Original failed receipts retained. Publication/ACP cases need the actual native fixture package; the missing environment error is retained and the correctly configured run is underway. Actual installed selected summary -> native commit -> original prompt -> ACP publication and refusal/UNKNOWN journeys follow before readiness. CI deferred.

Scoped source claims: follow-up to merged371; no HistoryViews/ThreadView373 implementation, no PiEvent/startup/watchdog edits. Selected native caller imports/API paths only. Resources: own .venv/.scratch tracked here, serial verification, cleanup after process-reference check.
