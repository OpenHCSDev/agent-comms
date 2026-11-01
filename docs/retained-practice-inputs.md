# Original-coordinate practice inputs — implementation checkpoint

Core #3829 remains unfinished. Entry, selected saved-context and coordinator input
evidence now have explicit PracticeCopy consumers in source. Complete coordinator
relations, coherent aggregate capture, bounded peak materialization, native-journal
reading and the prepared-root application run remain unfinished.
This document records the source decisions, not an executable selection roster.

## Original recipient birth participates in owner matching

Source tracing found `StoredInput.matches_owner` consulted only its origin,
ignoring the recipient birth already present in `OwnedBusInputKey`. For a neutral
origin, a recreated same-name owner could therefore satisfy that predicate even
when the input key named another original birth.

`InputKey.matches_owner` now owns the key's contribution. Birthless keys add no
birth constraint; the owned bus member checks its original birth through the
incarnation's existing `recorded_or` behavior. `StoredInput` combines that answer
with the unchanged origin-owned author/name check. No key is re-encoded, missing
birth inferred, generation substituted, lifecycle changed or native permission
created. This tightens an existing consumer, not a second retention-only identity
resolver. The added ordinary owner/recreation/birthless-owner and queued-text
refusal scenario is authored **UNRUN**; the existing birthless-key case remains.

## Existing owners and implemented entry selection

`ContextStore.retain_contexts(destination, identities, maximum_bytes=..., requests=...)` replaces the
entry-only API. `PracticeCopy.include_contexts` consumes it explicitly; normal
whole-file include and live-root backup selection are unchanged. Selected writable
SQL values travel through the existing attached source, without decode/re-encode,
coordinate relocation, source-identity minting or a compatibility entry point.

`ReconciledFile.retaining_rows` owns fresh staging, exact source-schema validation,
the existing load-before-guards transaction, repeated declared-dependency expansion,
and exclusive atomic publication. A refusal or failed closure never overwrites an
existing destination or publishes the partially loaded staged database.

`ContextRequest.retain_required_rows` owns its serialized version IDs, segment
entry references and earlier request IDs. Missing originals and foreign-thread
predecessors refuse. Whole retained predecessor records receive their own closure,
not an incomplete predecessor that only one selected segment can read. The original
ContextStoreInfo is required when requests are selected; entry-only selection does
not invent one. `RowidSequenced` retains SQLite's original sequence high-water mark
so an omitted original ID cannot become the next newly allocated ID.

The earlier entry-only API's owner-path run retained 6,274 entries in 13,877,248
materialized bytes with matching source-before/retained/source-after row digests.
It was not rerun on this implementation. Source compilation is not qualification
of the selected saved-request browser, atomic publication, rollback, sequence
metadata, resource bounds or native/physical behavior.

## Foreign-key inspection prerequisite

Previously `TypedTable.references()` exposed only table-level constraints. Column
references, including inherited/generated columns, live on `_columns` through
`Column.references`. Reading only the former silently misses edges such as
`NativeRuntimeInput.execution_id` and `owner_lookup`.

The new `TypedTable.foreign_keys()` derives both forms. `Reference.foreign_keys`
requires an answer from the reference kind: `NoReference` contributes no edge,
while `TableReference` binds its declared target to the source column. Composite
`ForeignKey` declarations retain their original objects and constraint options.
There is no table-name roster, SQL parser, alternate codec or stored graph.

SQL generation remains unchanged: column clauses and table constraints keep their
existing placement, so this inspection API does not change a stored schema or
require a carry. This is a prerequisite API, not a wired retention selector, and
has no application acceptance yet.

## Attached-source raw-row primitive — source only

`AttachedPracticeRebuild.retain_rows` selects original writable SQL columns and
inserts them unchanged into its bound destination connection. The existing
`PracticeRebuild.attached` context supplies that connection with the active source;
a mere path descriptor cannot select another attachment through a caller-supplied
database. A declared primary key controls repeat selection; a missing key refuses.
Other constraint conflicts are not hidden by `INSERT OR IGNORE`.

`ForeignKey.retain_parents` puts the original parent columns on the left of the
comparison, preserving SQLite parent affinity/collation and NULL-key behavior.
Internal aliases derive from the target declaration and cannot shadow its outer
table name. `TableSchema.retain_referenced_rows` expands one pass through declared
edges, including column references and required groups supplied by row overrides.
It is not a fixed-point or complete semantic closure.

`retain_keys` reuses TypedTable's indexed key-batch compiler. It shares raw insertion
and primary-key deduplication with `retain_rows`; no second key encoder or raw-copy
procedure is added. Completed changes come from the connection, since SQLite's
WITH-prefixed INSERT reports Cursor.rowcount=-1.

ContextStore and CoordinationStore now consume the primitive through their
explicit PracticeCopy selection methods. Guard-safe loading, required children, source snapshot consistency,
producer metadata, sequences, schema compatibility, materialized bounds and copy
behavior remain unqualified. No original lifecycle is rewritten and no full-copy/backup behavior
is changed. These methods are implementation prerequisites, not a runnable root.

## One attached source for metadata and rows — source only

`PracticeRebuild.attached` previously opened a separate read-snapshot connection
for producer/last-sequence checks while raw copying read the destination's SQLite
attachment. Under WAL those connections could observe different source revisions.
It now obtains the actual `SQLiteDatabase` through SQLite's database-list reader;
metadata checks and copies address that attachment on the same connection. The
existing reconciliation owner holds their write-transaction lifetime.

`SQLiteDatabase` composes `TableSource` and owns qualified table names. The default
source preserves ordinary SQLite name search, including legitimate temporary
relations; it is not silently changed to an explicit `main` prefix. The existing
TypedTable query owner supplies this source to select/stream, indexed key batches,
counts, existence and catalog checks. Producer checks and the sole bus identity
prefix consumer use it. No second connection, SQL parser or query codec remains.
The source argument qualifies the selected table, not arbitrary SQL subqueries:
joined relations still need their declaration-owned source qualification.

The FK predicates deliberately retain explicit `main` references: those are the
selected destination children/parents, not another original-source alias. The
original row still comes through `AttachedPracticeRebuild.retain_rows`. Replacing
those destination references with the original source would change the selection.

The existing rebuild API forwards the same transaction connection through schema
reconciliation; that parameter is not an independently maintained source snapshot.
No additional rebuild hierarchy or unchanged-whole-file gate was added. Actual
WAL interleaving, source/target isolation and producer-prefix controls are still
unrun; the coordinator consumer below is a source implementation, not qualification.

## Shared loading lifetime and acknowledgement groups — source only

`TypedTable.loading` owns table creation, the load interval, then installation of
that row's indexes/guards. Existing `rebuild` consumes the same sequence rather
than keeping a separate loading procedure. NRA's ModuleSyntaxIndex, StatementSource
and exact SourceTextPatch selected the original statements and preserved their
source geometry. This is an authored context composition, not a native equivalence
certificate or a class-member promotion.

`TableSchema.loading_rows` opens one transaction with FK enforcement suspended,
creates every table before yielding, installs every row's guards after loading,
then checks references before committing. Exceptions roll back through the existing
SQLiteFile transaction owner. The same `require_related_rows` check is used by
ordinary schema reconciliation. No ordinary runtime insertion guard is weakened.

`TypedTable.retain_required_rows` supplies the default empty contribution.
`RetainsChildren` composes that contribution with inverse **declared FK edges**.
ClaimBatchAcknowledgements inherits this capability because its sealed counts
describe the complete original child group; its forwarding override is deleted.
HarnessDelivery inherits the same behavior for its original observation parts.
Only a requested parent expands its children, not every participant's history.
`ForeignKey` owns both directions and their one parent-affinity comparison.
Original sealed state is copied unchanged; no seal transition or replay is issued.

These are source implementations, not executed copy/rollback/guard controls.
The acknowledgement and harness rows share the same retained-group capability;
actual class creation/MRO, schema stability and group-copy behavior still require
qualification. Four new harness-group scenarios are authored, UNRUN: selected
observations with an existing post-handoff claim, observations with no claim,
full-reference conflict, and copy conflict with no partial publication.
The coordinator seed consumer below now selects through the existing input
projection. Aggregate capture and the remaining serialized/cross-store and producer
dependencies remain OPEN; complete FK validity
would not establish their preservation or the original group's validity.

## Required preservation that SQL foreign keys do not establish

* **Uncertain inputs:** `InputDispositions.unresolved(entries)` joins original
  ledger state with `harness_attempts(entries)`. `stored_unresolved()` is not an
  equivalent seed. Stored rows in every state suppress a synthesized attempted
  key; copying only unresolved ledger rows could resurrect a completed attempt.
  No synthesized `ReservedInput` is written back merely to construct a copy.
* **Harness claims and observations:** `WakeAssignment.for_harness_sources` now
  owns recipient/sequence candidate selection and full MessageReference conflict
  checking. Normal settlement consumes it rather than maintaining its own SQL
  predicate; selected HarnessDelivery retention reads through the same active
  original attachment and copies every existing matching claim, including its
  unchanged lifecycle/revision. Missing claims stay absent: an attempt can precede
  claim acceptance. A same-sequence/different-message conflict refuses instead of
  silently disappearing. This natural relation is not fabricated as a mandatory
  FK. HarnessObservedPart's declared FK supplies its parent, not the reverse
  observation set; RetainsChildren supplies that original child group through the
  shared schema traversal. Original sender/body/record-ID/timestamp and partial
  ReceivingMonitor state are not reconstructed, completed or replayed.
* **Sealed acknowledgements:** `ClaimBatchAcknowledgements.column_guards` requires
  complete member/delivery sets and their original counts. Forward FK closure
  does not supply those children. Child insert guards reject an already-sealed
  parent: loading must not temporarily rewrite the retained sealed state.
  `TypedTable.rebuild` already distinguishes loading writable data from installing
  guards; the retained-copy implementation must use that owning mechanism.
* **Native source membership:** `NativeInputExecution.source_membership_sql` and
  `source_assignment_ids` own full/triage membership. Selection must consume that
  behavior rather than maintain another list of stages or link tables.
* **Producer/publication evidence:** executions, attempts, obligations, immutable
  intents, receipts and append dispatches carry original state and cyclic or
  generated-column references. A lifecycle is not reset to make insertion easy.
  The complete child/serialized-reference closure is still OPEN.
* **Serialized references:** `AnnotationRequestsRow.request` contains the original
  `DisclosureRequest.grant` message reference and disclosure state. Its submitted
  outcome means never resend automatically. `TranscriptRoute`/`InputDisplay`
  routing, native context proofs, entry references and saved-request predecessor
  links also require their existing value/store owners; absence from an FK list
  is not evidence that these relations do not exist.

## Unresolved-input seed projection

`HarnessDelivery.unconfirmed_for` now owns the incarnation/outcome query.
Membership derives from `Outcome.members_with(UnconfirmedOutcome)`; requested
keys are encoded by the declared columns through `TypedTable.select_keys`, not
hand-written outcome names. Normal input reads use that same query.

`SnapshotInputDispositions` inherits the unchanged `unresolved(entries)` and
all-state suppression algorithm. Its sole override reads harness rows through an
active `AttachedPracticeRebuild` source on that attachment's connection. It has
no new file/schema declaration. Its `root` must contain the retained bus and
complete ledger, captured consistently with the coordinator source; the caller
owns that capture and attachment lifetime. This is not a second ledger or an
input replay operation.

`SnapshotInputDispositions.retain_unresolved` now consumes that unchanged reader
and seeds original attempt groups at the selected keys. It asks the existing
harness producer a second time on the same attachment, not a copied key/outcome
or suppression algorithm. An unresolved stored ledger row can keep an additional
original attempt witness at its key; the complete ledger still wins the read.
The recipient lookup comes from those original source entries, not from a stored
name/generation or a fabricated birth. `HarnessAttemptOrigin.retain_harness_rows`
owns its bridge/reference selection and full-sequence check; origins without a
harness witness contribute no harness rows. No synthesized reservation is stored.

`CoordinationStore.retain_inputs` runs these seeds through the existing
`ReconciledFile.retaining_rows` loading/dependency/publication lifetime.
`NativeRuntimeInput.retain_sessions` additionally selects original native groups
by both recorded session id and original session path. A bare ledger native id
is not treated as a coordinator FK. The existing row hooks retain the selected
original children, claims and forward references.

`PracticeCopy.include_inputs` requires existing bus/ledger sources to have been
captured completely through `include`; a copied reader with no original source
refuses. The coordinator uses the remaining aggregate allowance, and its resource
is recorded only after successful publication. Ordinary whole copies/backups are
unchanged. The caller still owns the consistent source cut and serial copy; this
is not a capture certificate or native launch permission.

Three new cases in `tests/test_harness_receipt_context.py` are authored **UNRUN**:
no ledger row, an original reservation, and an actually bound/started ledger row.
They use the existing native-root/message/claim producers, compare the ordinary
source and retained projections and original attempt rows, and check the source
coordinator bytes and bus revision. Together with the eighteen previous cases,
**twenty-one added scenarios remain unrun**. These do not cover every disposition,
native association or rollback/resource failure.

Runtime class creation/MRO/registration, attached SQL, cross-store capture and
complete preservation remain unqualified. The selector has no participant-wide
reverse walk; current participant/pointer aggregates, inverse native membership,
serialized references and the remaining original recovery groups still require
semantic closure. No seed-only database is a runnable root or complete backup.

## Copy and backup are different contracts

`PracticeCopy.include` currently invokes each `RootFile` owner's whole-file
`copy_group`. At the checkpoint baseline, `PracticeBus.select` returns the same
input union for rehearsal and live backup. A bounded subset must not be labelled
as a complete original-store backup. Keep ordinary copy/backup behavior intact;
represent a bounded selection explicitly through the existing request/copy
owners. `PracticeCopy.include_contexts` is an explicit selection API; its request
continues to describe original-to-retained path mapping, not a complete-backup
certificate. BackedUpBus still constructs LiveFilesRequest for the original live
root, rather than backing up the selected practice bytes. Do not add a parallel
importer, ledger or reminted session identities.

Measure the complete selected stores, WAL/copy groups and free space before any
copy. The requested production-copy proof is bounded to 3 GiB; zero freelist pages
in the oversized stores means compaction is not the repair.

Selected context materialization now requires an explicit byte allowance. Through
PracticeCopy this is a **total private-tree allowance**, not a new default for full
backups. `PracticeMeasurement.capture` is the one tree-byte calculation: already
retained files, WAL/companions and staging files count whether or not the copied
resource index lists them. `PracticeCopy.remaining_bytes` subtracts those bytes and
also limits new allocation to actual disk space above the existing StorageRule
floor. Its caller owns serial materialization of the private root and the coherent
source cut; this is not a reservation against other processes consuming disk.

`SQLiteDatabase.limit_bytes` consumes SQLite's actual page size and accepted
max_page_count through typed pragma records. It caps the staged database before
schema/rows/indexes are allocated; a zero-page allowance refuses, and SQLite can
raise SQLITE_FULL on later growth. `ReconciledFile.retaining_rows` additionally
counts the complete closed staged file group before exclusive publication. An
over-budget group refuses and the same staging cleanup runs. No cap, page size,
sequence, schema or lifecycle is written into the original store.

This is authored admission/allocation code, **not a measured peak-footprint or
free-space race proof**. Journal/temp-spill peaks during loading, concurrent disk
consumption, rollback/cleanup under SQLITE_FULL, and the complete cross-store
selection/root budget still need qualification. Ordinary include()/PracticeBus
and live-root full backups remain unchanged; a caller cannot label their
unmeasured union bounded merely by passing an allowance to one selected store.

## Native membership and execution groups

A triage member's `assignment_id` is unique but has no declared assignment FK.
Forward FK closure alone therefore misses its original source claim. The
existing `NativeInputExecution.source_membership_sql` and
`source_assignment_ids` own this relationship for both TRIAGE and FULL. Their
queries now take the existing `TableSource` through every nested join; ordinary
calls still use normal local-table lookup. The membership query leads a left
join into the existing required `WakeAssignment` decoder, rather than silently
filtering a missing original claim through an inner join. That failure path is
source-reviewed, not yet executed.

`NativeRuntimeInput` composes `RetainsChildren` for original triage/cursor
children and overrides the existing cooperative retention hook to consume its
held execution's original-source assignment query. It uses the shared raw-key
retainer for the claims; it does not write a second membership query, stage
roster or new native coordinate. `ExecutionRecord` composes the same capability
for its original declared child group, including native inputs, attempts and
links. No participant-wide reverse walk, lifecycle transition, dispatch or
claim acceptance is added. No field/DDL/carry declaration changed; actual schema,
MRO and load behavior remain unqualified.

Three new scenarios are authored **UNRUN** in
`tests/test_retained_coordinator_groups.py`: selected triage keeps only its own
membership/claim; an execution seed keeps its original claimless FULL input;
explicit original-source lookup differs from empty destination-local lookup.
They use existing native-root/schema preparation and typed store owners, not a
handwritten table layout or fake native provider. These are SQL data-group
cases, not actual native admission or runnable-root acceptance. FULL wire
membership, malformed-source refusal, cursor/attempt/publication groups and
all-state suppression still need the necessary combined behavioral selection.
That checkpoint added thirteen retention scenarios, all unrun in the source pass.

The focused NRA before-map covers nineteen production modules and219 raw/static
class declarations with no parse omissions. The final delta additionally covers
the changed test module; counts are not evidence of runtime MRO, complete
cross-store closure or a delivered root. Complete paginated open-PR paths were
checked, including712/249-file requests. Published3820's superseded
`model_contexts_for` hunk is independent; its deletion stays, and the retention
hook has no `BusReader`/`MessageReference` dependency.3614's relevant changes are
nominal-import moves already present here. Source recipes/results are retained
under `p867-native-group-*`.

## Publication groups and the existing capture lifetime

`PublicationIntents` now composes the same `RetainsChildren` capability as
`ExecutionRecord`. The schema's declared inverse relations retain the original
`PublicationEvidence` and `PublicationAppendDispatches` rows. The latter's foreign
key is declared in `coordination_response.py`, not in a retention-only roster.
`RecoveryReader.get` reads the original intents and receipts separately;
`PublicationAppendDispatches.resolve_retained` uses the original append record.
Copying the intent alone does not preserve those answers. The raw loader retains
absence as absence: no receipt, dispatch, acknowledgement or state transition is
created to fill a missing record.

Four publication cases are authored **UNRUN** in
`tests/test_coordination_response.py`: intent/execution seeds crossed with frozen
intent/published response. They reuse the existing actual response preparation
and publication path, compare original intent/receipt/append rows, and check
that retention changes neither the source coordinator digest nor the bus
revision. The existing fixture now declares its actual `AssignmentId` and
`OwnerFence`, rather than `str` and `object`. These are stored SQL-group controls,
not runnable-root, native, recovery-pointer or physical acceptance. Together with
the earlier thirteen retention scenarios and one recipient-birth regression,
those eighteen scenarios remain unrun; the later consumer adds three more above.

The live installer already enters `mode.owners_stopped` before its practice
phase (`installer/upgrade.py`). `LiveMode` uses the existing maintenance hold,
stopped-owner and retargeted-runtime lifetime. `RehearsalMode` instead calls
`owners_kept` (`install_choices.py`). A plain rehearsal therefore does not supply
that quiet source boundary. The selected coordinator consumer must use the
existing qualified capture lifetime and preserve its registry/complete-ledger/
bus/coordinator relationships; this source trace neither executes nor certifies
that aggregate capture. No new freeze state, installer or donor operation was
added.

## Source evidence and remaining work

The retained NRA source map at baseline `095c4cdfe85632f990be3b6d67411d9685e6dee9`
covers 632 Core `src` modules, 6,152 original class declarations/projections,
105 table-family source declarations, 7,263 annotations and 48,319 call sites,
with no parse omissions. The composition-derived projection contains 35
declarations, including family/schema bases, not 35 concrete tables. It follows
`PrivateCoordinatorSchema`'s actual schema parents, including `AwarenessTable`:
a narrower coordinator/private-only query missed awareness's two declarations.
Exact hashes and source spans are retained in the owning run's
`p867-coordination-owner-map.json` and
`p867-coordination-composed-declarations.json`. The earlier narrower query is
retained as counterevidence, not used as complete schema coverage.

Call spellings and nominal projections do not prove runtime binding or semantic
closure. External/dynamic bindings, serialized links and required reverse children
remain OPEN. The earlier d4/Toad corpus and its unresolved cases are separate;
these counts do not resolve them. New metadata methods require targeted reproof.

The current post-P859 selector slice covers 30 production/test/fixture modules
and 585 original/canonical class declarations, with no parse omissions. It extends
the 22-module/524-class before-slice through four fixture/test modules and four
moved-owner modules. The intervening 26-module/525-class result keeps its older
source epoch. Compilation and exact NRA-indexed AST comparisons preserve all 148
existing functions in the six changed Python files. Five mapped dependencies
changed on rebase: registry resolution declarations, bus selection imports, the
FieldCodec compared-field/decoder owner, and two fixture import moves. Those
changes were read; they are not inherited runtime qualification. These are source
checks, not native transport or SQL execution.

Next: finish the remaining coordinator semantic groups and qualify coherent
aggregate capture and peak budget, then the combined backend/carry and actual
prepared-root saved-history journey. No pytest, provider, donor mutation or new
native/physical run was performed for this source checkpoint. Reader promotion
still requires the separate default-storage and decorator creation-cut proofs.
