# Final fork/cut caller closure

Current source: 99c60fa7b1ac93500be2a04e7313b035e3e7a762.
Delta from integrated main checkpoint23e7d82b: 259 production lines added / 41 deleted in 11 files (including native producer patch). Test and evidence lines are excluded.

| Fact or effect | Existing owner and migrated consumers |
| --- | --- |
| Locked SDK source copy, child creation and exact written bytes | SessionManager.forkFrom emits its creation hook after file/parent fsync while it still owns the creator lifetime. Native helper returns only that typed receipt. |
| Creation publication before any child input | PrivateInputs.fork verifies then inserts the SAME NativeForkCreation result once in the existing CompactionJournal. ThreadManagement calls it before declaring/launching the child. NativeForkCreation composes existing session identity, revision/digest and journal/table capabilities. |
| Fork consumers | ThreadManagement plus shared_bus_restart_native, turn_context_installed_journey, selected_summary_recovery_installed_journey, compaction_source_successor_installed_journey, reply_relevance_installed_journey, real_native_compaction_installed_pilot, native_context_budget_installed_journey and test_native_context_inspection all invoke the journal-owned creator. Only PrivateInputs invokes ForkSessionHelper.run. |
| Inherited source coverage | NativeEvidenceRead obtains NativeForkCreation.recorded_prefix through its acquired journal transaction. Original path/header/parent/inode and initial byte prefix must match. Copied managed markers are inside that authenticated prefix, never interpreted as child commits. |
| Later committed cut | ManagedCompactionEntry composes NativeSummaryPayload, mandatory NativeCommitIdentity and original SummaryFiles/marker-only metadata. CompactionOperation corroborates the exact child witness/outcome and existing ancestry. NativeEntry boundary alone selects the wire member. |
| Continued private input | verify_continued_private_session retains every raw/UNKNOWN proof obligation, refuses later untracked users and requires current reserved source revision. Coverage never grants/retries an unresolved input. |
| History enrollment exclusion | SessionJournalHistory derives membership from existing typed table declarations; both pristine creation and continued coverage use it. Empty fresh-file enrollment remains its separate original O_EXCL contract. |
| Format carry | Existing stopped NativeSchemaCarryPlan derives the new table EMPTY. No new store, reset, proof rewrite, legacy reader or retrospective fork enrollment. Original201 has no creation witness and remains preserved. |

The AST source map parses311source/357test/53tool modules with zero parse omissions. Native callback delivery and dynamic dispatch are not established by Python AST; the matched installed artifact and configured fresh-fork journey address those boundaries. Source sanity70passed/2optional skips. Installed core wheel/assets equal this source; SDK0.12.1 and native9f12 manifest/tree qualified. Actual configured run is currently pending in /home/ts/wt/a520s202; this receipt does not claim optional timing ready.
