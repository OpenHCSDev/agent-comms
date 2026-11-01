# Original-coordinate practice inputs — implementation checkpoint

Core #3829 remains unfinished. Entry-manifest retention exists; bounded coordinator
retention, native-journal reading and the prepared-root application run do not.
This document records the source decisions, not an executable selection roster.

## Existing owners and implemented entry selection

`ContextStore.retain_entries(destination, identities)` streams `ContextEntry` rows
through `TypedTable` under the source snapshot and destination write lifetime.
Original session/file/item keys and manifest values remain unchanged. It refuses
an existing destination. It does not copy `ContextRequest`, segment versions or
store identity and therefore does not qualify the saved-request browser.

The earlier owner-path run retained 6,274 entries in 13,877,248 materialized bytes;
source-before, retained and source-after row digests matched. No native or physical
acceptance follows from that result.

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

This new primitive is not yet consumed by CoordinationStore/PracticeCopy. Guard-safe
loading, required children, source snapshot consistency, producer metadata,
sequences, schema compatibility, materialized bounds and copy behavior remain
unqualified. No original lifecycle is rewritten and no full-copy/backup behavior
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
unrun, and no CoordinationStore/PracticeCopy consumer is claimed.

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

`TypedTable.retain_required_rows` supplies the default empty contribution;
`ClaimBatchAcknowledgements` overrides it because its sealed counts describe the
complete original child group. The schema follows inverse **declared FK edges**
only for that requested parent, rather than expanding every participant's history.
`ForeignKey` owns both directions and their one parent-affinity comparison.
Original sealed state is copied unchanged; no seal transition or replay is issued.

These are source implementations, not executed copy/rollback/guard controls.
CoordinationStore/PracticeCopy has no selection consumer yet. Natural-key harness,
serialized/cross-store and producer dependencies remain OPEN; complete FK validity
would not establish their preservation or the original group's validity.

## Required preservation that SQL foreign keys do not establish

* **Uncertain inputs:** `InputDispositions.unresolved(entries)` joins original
  ledger state with `harness_attempts(entries)`. `stored_unresolved()` is not an
  equivalent seed. Stored rows in every state suppress a synthesized attempted
  key; copying only unresolved ledger rows could resurrect a completed attempt.
  No synthesized `ReservedInput` is written back merely to construct a copy.
* **Harness claims:** `HarnessDelivery.settle_claims` and
  `AssignmentStore.settle_harness_inputs` relate the original recipient and
  message reference to claims. `HarnessDelivery.references()` declares only its
  participant dependency. The natural-key claim relation is not an FK edge.
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

## Copy and backup are different contracts

`PracticeCopy.include` currently invokes each `RootFile` owner's whole-file
`copy_group`. At the checkpoint baseline, `PracticeBus.select` returns the same
input union for rehearsal and live backup. A bounded subset must not be labelled
as a complete original-store backup. Keep ordinary copy/backup behavior intact;
represent a bounded selection explicitly through the existing request/copy
owners. Do not add a parallel importer, ledger or reminted session identities.

Measure the complete selected stores, WAL/copy groups and free space before any
copy. The requested production-copy proof is bounded to 3 GiB; zero freelist pages
in the oversized stores means compaction is not the repair.

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

Next: implement the declared-reference traversal and owned semantic groups,
consume them in the bounded copy request without weakening full backups, then
qualify the coherent implementation through the combined backend/carry and actual
prepared-root saved-history journey. No pytest, provider, donor mutation or new
native/physical run was performed for this source checkpoint. Reader promotion
still requires the separate default-storage and decorator creation-cut proofs.
