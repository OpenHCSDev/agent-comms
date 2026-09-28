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

Historical retained journal fixture converted and reopened: 97 raw UNKNOWN rows and one
refused summary, no operations/publications/enrollments. Explicit SQLite state-family acceptance passed (2 tests): every current operation,
summary and publication family member; preserved enrollment and raw UNKNOWN identity;
current native_input_admitted barriers; duplicate unresolved ID refuses replay;
unrecognised extra tables refuse conversion. No claim that a refused summary is settled.
The receipt's unresolved_summaries counts nonterminal states, not every send barrier.

Parent must install this current journal instead of deleting compaction barriers.
No runtime source edits or edits to parent cutover tools. One-shot code must be removed
following successful D22 installation. Branch starts at PR251's accepted58bfad3.

## Completed acceptance

`family-fixed.txt`: 2 passed in 0.29s, actual SQLite files and current journal/send
admission methods. `retained-barrier.txt`: actual 97 UNKNOWN IDs and one refused
summary preserved, source bytes unchanged, retained session still refuses input.
No provider calls, live edits, operation transitions or reconciliation. Initial
family-codec discriminator error and a case-sensitive test regex error are retained
in earlier receipts and corrected; no broader suite pass is claimed.

`goal-private-inventory.json` records the actual nested database inspection.
See GOAL-PRIVATE.md for the parent-owned reset/evidence disposition.

This PR adds a temporary converter and its boundary acceptance rather than changing
runtime source. Remove the tool after the real D22 installation succeeds.

## Live correction received at 19:12 UTC

Parent evidence `~/wt/comms-live-unread-pin-20260928/evidence/refusal-retirement/result.json`
records the exact CAS retirement of the previous `limit_exceeded` refusal. Current
live state is 97 raw UNKNOWN rows plus one `retired_refusal`; original UNKNOWN
unchanged and send admitted. Zero provider calls, submitted inputs or owner restarts.
The earlier retained-barrier receipt is historical. Conversion preserves the
current retired state using its existing declaration; family acceptance already
covers RetiredRefusalSummary without replay or inventing an admission token.
