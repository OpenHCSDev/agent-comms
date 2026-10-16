# Checkpoint-owned resource claims

Owner: core-goal-retry. Base: 46ee4a206a28dfb97ff93ed4d839436cda4b5261.
Task: Claude11434. The indexed owner path is implemented and exercised through
the actual public Comms API. Installed claimed-edit and latency acceptance remain
unfinished; this is not an installed fix.

## Existing failure and complete reader family

`StoreLock.claim_transitions` (`store_files.py:141`) materializes every certified
message, then extracts every transition. `publish_selected_resource_claim`
(`claim_admission.py:190`) replays all transitions to ask about one resource and
walks them again to ask about one operation ID. Publication repeats the replay
(`publisher.py:264`), and selected write verification repeats it
(`claim_admission.py:349`). Exact terminal release repeats it
(`coordination_snapshot.py:346`). These are four external StoreLock call sites.
`WireLog.claim_projection` (`wire_log.py:451`) independently scans raw history.
StoreLock also has a recursive refresh call and a no-checkpoint whole-bus branch.

The checkpoint already owns `_index_row` inside the same SQLite transaction as
prefix certification. `MessageWireRecord.checkpoint_rows` owns declaration-derived
message indexing; its existing `TypedTable.insert` dispatch is the commit hook.
`Column.generated` already derives indexed SQL facts from typed stored values.
No new codec, index registry or second claim file is needed.

## Facts and determining owners

- A current resource owner is a resource-to-original-claim reference, maintained
  only by checkpoint commit. Indexed reads must not decode bus messages.
- A consumed operation ID lasts beyond resource release. A current-only table
  loses that fact when a claim is deleted. Also, ClaimTransition allows multiple
  resources in one admitted envelope: a unique ID on every resource row would
  reject a valid envelope. Do not introduce either cardinality mistake.
- Keep the immutable typed ClaimTransition once in a checkpoint ClaimCommit row.
  Derive its unique sequence and unique optional operation ID through existing
  generated columns. ResourceClaims rows contain only normalized resource and
  a reference to that original commit. Both relations are in the one certified
  checkpoint, written in its one transaction; neither is another store/cache.
- Move the existing pure application algorithm onto ClaimTransition. Preflight
  and checkpoint insertion consume that same behavior against only the affected
  resource subset. Preserve exact incarnation/generation release, rename behavior,
  whole-set rollback and synchronous ClaimConflict/IdentityConflict before append.
  Append uncertainty stays UNKNOWN; it never licenses replay.
- StoreLock owns refreshing/lending a current certified read while retaining its
  physical bus lock. It must refuse a missing checkpoint instead of scanning.
  Resource, operation and explicit current-claim queries belong to CertifiedSourceRead
  and their checkpoint row types. Genuine full current inspection is not a bus scan.

## Migration closure and durable cutover

Migrate all four StoreLock consumers plus WireLog inspection; delete the old
StoreLock method, replay-all helper and unused message transition roster accessor.
Use TypedTable for SQL boundary work, including keyed removal shared with checkpoint
recovery. Do not leave another implementation of claim conflict/release rules.

An explicit tool outside src plans by default, validates the original sealed
source, and carries claim rows through the existing canonical checkpoint installer
only after old writers stop. It preserves bus bytes and non-seal marker facts.
The new runtime rejects an old checkpoint schema; no load-time old-shape reader
or scan fallback remains. Delete the carry tool after reviewed cutover and acceptance.
No live root changes are authorized by this source checkpoint.

## Source evidence and confirmation still owed

The existing NRA parser inventoried 1,064 Core/test/tool/Toad/dependency modules,
zero omissions, retaining every class/write/decision/import/call site. Its canonical
class-family index joins all 2,511 original class declarations across 336 Core and
metaclass-registry modules, with zero unmatched declarations in that corpus.
Python syntax/bindings do not prove runtime monkeypatches, metaclass effects,
SQLite atomicity/query plans, native socket execution or OS fsync semantics.
Before/after evidence lives under the owned persistent scratch directory
`/home/ts/.cache/agent-scratch/core-goal-retry/checkpoint-resource-claims`.

The authored public Comms journey acquires, refuses a competing claim before
append, releases, reacquires through the other owner and reopens the same current
state; no provider/native input is created. Remaining confirmation: competing acquisition,
operation reuse after release, valid multi-resource envelopes, exact release,
rollback/reopen and crash certification; then the actual installed claimed-edit
entrypoint and retained history on an isolated root. Measure wire-lock hold time
before/after against a safely copied large bus and report the actual size. Critical
host disk/swap pressure currently prohibits an unplanned 5.5 GB duplicate or fleet.
Use an existing safe copy/reflink or obtain headroom before a large run. Preserve
original sessions, auth, wire data and UNKNOWN inputs; never replay them.
