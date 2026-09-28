# One-shot compaction journal preservation

Callable: `tools.cutover.compaction_journal.stage(source_database: Path, destination: Path)`.
Destination must be a new directory with an existing parent. Result is JournalRewrite;
install `destination/compaction-commits.sqlite3`, retain `original-compaction-commits.sqlite3`
as offline evidence. Parent owns current_root/install_root wiring and sidecar removal.

The converter uses current CompactionJournal/TypedTable declarations and state families.
Every operation ID, intent/evidence string, publication outcome, selected source/state,
raw input UNKNOWN ID and enrolled session identity is retained. Old admission_epoch is
renamed at this one-shot boundary only. Inode evidence is NOT rebound; copied sessions
must not inherit native proof or fresh enrollment capability. No journal transition,
provider dispatch, reconciliation, returned terminal ACK or admission token is minted.

Actual retained journal already converted and reopened: 97 raw UNKNOWN rows and one
refused summary, no operations/publications/enrollments. Explicit unresolved-family
fixture acceptance is in progress; no claim that a refused summary is settled.
The receipt's unresolved_summaries counts nonterminal states, not every send barrier.

Parent must install this current journal instead of deleting compaction barriers.
No runtime source edits or edits to parent cutover tools. One-shot code must be removed
following successful D22 installation. Branch starts at PR251's accepted58bfad3.
