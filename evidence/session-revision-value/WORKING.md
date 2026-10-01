# SessionRevision whole-surface working checkpoint — #468

Sole implementation owner: Mendel. Release integration: parent.
Base `ca9a1ab3244a31ac5db3877d9a53210edaa6f589` (main9370; production970).
Patterns: **IDEN-3, BOUND-1, TIME-9**. No frozen release or public owner changed.

## Required relation and implemented owners

`selected_source.SessionRevision` owns one original `private_path.FileRevision`
and its named `MissingInputProofRevision` / `PresentInputProofRevision`.
`UnavailableSessionObservation` represents an unsuccessful observation;
it cannot match a reserved value or supply one. Only FileNotFoundError on the
sidecar means missing; other OSError results refuse acquisition, preserving
their cause. Stats do not enroll a native session or prove an input/retry.

The value owns whole current equality, unchanged input-proof comparison,
same-inode native cut/append bounds, and the exact external five-field colon
stamp. The native stamp excludes the proof journal; ctime is metadata change,
not process birth. Original custody and proof readers remain authorities.

## All direct consumers

| File | Complete replacement relation |
| --- | --- |
| compaction_outcomes | First consumer: named native size, delegated inode/cut placement, original value encoded through FieldCodec in scoped outcome digest. |
| selected_source | Sole observation and value/presence declarations; strict acquired source field. |
| backend | Deletes private tuple aliases/readers; actual live child + acquired observation required for retention. |
| native_custody | Existing retained child asks its captured value whether source is current. |
| continued_private_session | Original value fences before/after complete native input/proof coverage; UNKNOWN checks unchanged. |
| selected_summary_admission | Current/decline/proof-preservation operations use value owner; public original-source accessor removes binder's private identity access. Token fsync/custody/lifetime unchanged. |
| reservation_rules | Observation matches original required revision; unavailable cannot authorize reservation/recovery. |
| compaction_summaries | Existing locked journal/input boundary observes current value, no second stat reader. |
| compaction_states | CommittedNativeOutcome consumes unchanged native five-field stamp. |
| selected_pi_summary_rpc | Indirect RetainedNative consumer validates the witness through the same native_stamp owner. |
| owner_compaction_adaptive | Requires an observed value before creating selected original admission. |
| owner_compaction_manual | Requires a value before encoding ManualSource; no None slipped through required annotation. |
| owner_compaction_commit | Interrupted and exact commit checks consume current observation; no uncertain-input replay. |
| turn_input_binding | Acquires current value and original reservation through capability's public owner; unavailable burns capability and refuses bind. |

Indirect `input_disposition`, `compaction_records`, `compaction_identity`,
`compaction_journal`, `transcript_outcomes`, `transcripts`, `transcript_receipts`
already invoke these original operations. No extra codec/store/copy needed.
All existing test consumer files, including the actual child script, are
migrated. No private revision imports, tuple subscripts or None state probes
remain in production or those fixtures. Old tuple JSON is rejected.

## Deletions, storage and checks

**75 production lines deleted / 155 added** across 14 owned modules.
Backend's tuple reader and duplicated encodings are removed in place.
Runtime `compaction-commits.sqlite3` nested source format resets at the owner's
quiet cutover. No legacy reader/alias/SelectedSourceCodec or converter.
Original native sessions, durable input-proof/UNKNOWN, wire/goals unchanged.

Source-only affected controls: **36 passed / 3.31s**, one inherited failure.
Exact untouched base reproduces the same late-outcome assertion in **1.00s**:
root `event.text == 'equal original bodies'` count is 0 versus expected 3.
Original outcome identities, frontier invalidation, read-only journal, paging
and no-rewrite checks preceding it pass. Parent identified invalid native string content and assigned the fixture correction
here. Canonical TextContent array passed in 0.19s; all original assertions remain.

One small noneditable wheel environment was built under persistent
`/home/ts/.cache/agent-scratch/sr468/installed`; all **294 package .py files**
match source. Unchanged native593 full package trust passed from the wheel's
manifest. Actual serial native/ACP summary/commit/reopen/failure acceptance is
pending; this is a working source checkpoint, not installed Ready.

Scratch owner: Mendel. Short private roots/env/wheel: `~/.cache/agent-scratch/sr468`;
logs, original inventory/baseline reproduction and provenance:
`~/.cache/agent-scratch/mendel-session-revision-468-20261001`.
Resource guard: home10.1GiB/RAM17GiB/swap8.9GiB warning. No env fleet,
paid provider, public input, owner restart or native/stack modification.

Native01 exposed the omitted RetainedNative positional consumer above plus obsolete
fixture StateData/context_size and double fresh-root initialization. The RPC
consumer is corrected, prepared model owns the fixture selection/capacity, and
already-certified private roots are read through the existing certificate.
Failed native01 roots/logs remain protected; no original input is replayed.

Current Ready receipt: `evidence/session-revision-value/READY.md` and READY.json.
This working-checkpoint narrative is historical; completed installed acceptance
is recorded there, including the real44.7MB manualACP journey and source-only
final docstring clarification. No completed gate was repeated.
