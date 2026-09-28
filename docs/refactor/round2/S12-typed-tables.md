# S12: Typed tables

**Head audited:** `agent-comms` `main` at `15a4d00` (#225); re-verify at yours. **Rules:** [00-RULES.md](00-RULES.md). **Builds** [A13 `TypedTable`](02-SHARED-ABSTRACTIONS.md#a13-typedtable); **uses** A2.
**Step 1:** build A13 as a new module. **Step 2:** migrate every table no other active surface owns.

---

## What is wrong

Every SQLite table's shape is written out several times: in its DDL, in its class where one exists, in each positional `INSERT`'s value order, and in every reader's hand mapping from columns to fields.

- **57 tables; 321 reads by column name or index across 32 modules**, led by `coordination_store.py` and `optional_awareness_projection.py` (44 each), `historical_native_inputs.py` (30), `compaction_journal.py` (20), `native_source_cursor.py` (18), `todos.py` (17), `coordinated_runtime.py` (14).
- **34 of 51 `INSERT`s are positional**, across 13 modules. Adding or reordering a column breaks them, or silently writes values into the wrong columns when the swapped columns share a type.
- **Classes that exist are rebuilt by hand at every reader:** `read_expected_prompt_binding` reads 13 columns into a `PromptBinding`, `_cursor_from_row` maps 12 into `CurrentNativeCursor`, `read_ordinary_delivery_candidate` maps all 7 of `OrdinaryDeliveryCandidate`'s stored fields one by one.
- **`native_runtime_inputs` has no row type at all**: 16 columns, one writer, eight modules reading it raw.
- **Join results have no types:** `optional_awareness_projection.py` reads projection rows whose shapes exist nowhere as classes.
- **Three columns keep a retired name:** `admission_epoch`, `owner_admission_epoch`, `sent_owner_admission_epoch`.

---

## Target

- **One row type per table**, a frozen dataclass on A2, and **A13 derives everything else from it**: the DDL (columns, keys, uniqueness, references, indexes and `STRICT`, declared as field or class metadata), strict reads, and inserts and updates with derived column lists.
- **Hand-written DDL is deleted.** So are the hand mappers (`_cursor_from_row` and every function like it) and every positional `INSERT`.
- **A row type for each projection query**, declared beside the query.
- **`NativeRuntimeInput`** declared as `native_runtime_inputs`' row type.
- **The retired names disappear by derivation:** the row types use `admission_generation` and its siblings, and the derived DDL follows.

---

## Cutover

Deriving the DDL changes schemas, so every table S12 touches is classified (rule 2):

- **Runtime and derived tables** (prompt bindings, native source cursors, ordinary delivery candidates, native runtime inputs, awareness projections, and the rest of the private N/K runtime state) **are reset** at cutover.
- **Durable tables** (for instance `todos.py`'s, if they hold the owner's tasks) keep their data through a one-shot tool in `tools/cutover/`, run once and then deleted.

No code in `src/` ever reads an old schema.

---

## Scope

**Every table in every file not owned by another active round-2 surface.** S9 migrates the compaction tables, S10 the tool broker's read, L0 anything inside its targets. Everything else is S12's, including `coordination_store.py`, `optional_awareness_projection.py`, `historical_native_inputs.py`, `native_source_cursor.py`, `native_prompt_binding.py`, `todos.py` and `coordinated_runtime.py`.

`ordinary_delivery_bridge.py` was deleted in #225. Its readers and table migration are removed from this scope; do not recreate the module. The historical counts above predate that deletion.

---

## Guards

In S12's files as soon as it merges, and codebase-wide once S9 and S10 have adopted A13:

- no reads of a row by column name or index;
- no `INSERT` or `UPDATE` without a derived column list, and no positional `VALUES`;
- no `CREATE TABLE` outside A13.

---

## Tests

- **One family-level test for A13:** for every registered table, the derived DDL creates a table whose columns match its row type, and a row round-trips. That single test covers all tables; there is no per-table test.
- **One new-case test (T2):** a test-only row type gets DDL, reads and writes with no other edit.
- **Delete** the per-table tests of hand mappers, positional inserts and old schemas. Do not port them.
- Behaviour tests of what the stores are used for keep passing.

---

## Done when

A13 is merged; every table in S12's scope has a row type, derived DDL and typed access; no hand mapper, positional insert or hand-written DDL remains in its files; runtime stores have been reset and durable ones carried across by tools that are now deleted; the guards pass.

## Dispatch

> **`refactor-s12`:** Complete S12 per `docs/refactor/round2/S12-typed-tables.md`. Read `00-RULES.md` first. Land A13 first as a new module; S9 and S10 are waiting on it. Then migrate every table in your scope completely, deleting hand-written DDL, hand mappers and positional inserts as you go. Classify each store before its schema changes: reset runtime state, carry durable state across once with a tool you then delete.

## Current implementation ownership

Foundation [#230](https://github.com/OpenHCSDev/agent-comms/pull/230) merged
at reviewed `cca3b282`. Full S12 is **in progress**, owned by
`refactor/round2-s12-caller-closure` in `~/wt/comms-refactor2-s12-20260928`.
Caller-closure draft [#237](https://github.com/OpenHCSDev/agent-comms/pull/237) follows #230; it must finish all remaining stores,
callers and deletion guards before S12 is complete.

- **A13 API:** frozen dataclass subclasses of `TypedTable` own table names,
  columns, strict reads/writes, constraints, references, indexes and triggers.
  `TypedRow.read(cursor)` decodes query projections. `one(db, **keys)` returns
  one declared row or None. Generated fields cannot be written.
- **Implemented continuation:** native input/cursor declarations and readers,
  prompt binding declarations, private sidecar metadata, and 15 coordinator
  table declarations replace their handwritten DDL. Coordinator mutation
  callers and rich lifecycle row consolidation remain in progress.
- **S10 dependency API:** `NativeRuntimeInput.one(db, input_id=...)`; attributes
  include `assignment_id`, `sent_owner_admission_generation`, execution/owner
  identity, stage and native proof fields. Physical table `native_runtime_input`.
  Pascal owns selected_tool_broker.py adoption. Parent owns history_views.py.
- **S9 dependency:** compaction owners adopt A13 after #230. No changes to
  their files without handoff.
- **Runtime reset files:** `coordination.sqlite3` (whole coordinator runtime,
  including 15 core tables, native_runtime_schema_meta, native_runtime_input,
  current_native_cursor); `native_prompt_bindings.sqlite3` (snapshot_meta and
  prompt_binding, including its pending intent/lock protocol). Parent must
  quiesce owners before resetting any of these; no reset was performed here.
- **Durable stores:** `todos.sqlite3` now derives its schema and all reads/writes
  from the existing `Todo` declaration. `tools/cutover/todos.py` stages every
  task, goal, revision, assignment and uncertain-retry identity once. A real
  prior-store -> current-store reopen check passed. Parent must install the
  stage at quiet cutover and delete the tool afterward; no live todo file was
  changed. Other durable stores remain to migrate, never reset as runtime.
- **Current caller closure:** participants, aliases, owner generations, pointers,
  recovery audits and replay observations now use typed writes/reads. Deleted
  the duplicate CurrentExecutionPointer, ReplayAssessment and PublicationIntent
  declarations: CurrentExecutions, ReplayAssessments and PublicationIntents
  are their sole row/behavior owners; all current references are updated.
  PublicationReceipt is a typed join projection. Remaining lifecycle mappers
  and coordinator writes are still open.
- **Delegated crossing:** Copernicus owns bus_route_counts.py and its A13
  conversion, routing.DeliveryScope and MessageBus pending_counts,
  pending_counts_all, inbox and _pending_route_fields. S12 does not duplicate
  these. Parent owns MessageBus history, WireMetadata access/floor and D22.
  Parent 0330ad9 must survive later integration unchanged in meaning.
- **Floor and D22:** parent #229 owns the required durable
  WireMetadata.admission_after_seq. Preserve parent 6fd9857 on integration.
  Selection and acceptance require seq > H. Current native proof stays 0
  until actual post-floor coverage. Plain retained Message rows <= H are
  history only; no invented old recipients or native proof.
- **Local acceptance:** real SQLite coordinator/sidecar/binding behavior checks
  include persistence, concurrent initialization, fsync failure and subprocess
  crash negatives. Native binding tests use synthetic Pi results; they do not
  establish installed provider acceptance. Parent owns real UNKNOWN/reset,
  D22 history and fresh-message activation proof. S12 stays open until those
  pass and every assigned caller/deletion guard closes.

### Reader closure in #230

`optional_awareness_projection.py` now uses declared typed join projections for
all SQL reads. Its source/owner/selected-claim/obligation guards still apply.
No persisted shape or admission cursor changes, so this reader adoption needs no
reset. A13 adoption guards derive their file scope from imports and prohibit raw
row extraction and hand-written DDL/writes; complete-table migrations expand that
scope automatically. Local family/guard/awareness checks: 25 passed.

### Reset admission requirement

Parent owns D22 and quiet activation. Runtime reset must preserve operation
semantics: capture pre-cutover highwater H in required durable
WireMetadata.admission_after_seq, then accept/select only seq > H. The existing
checkpoint is index progress, not a native proof cursor. UNKNOWN remains history,
never a fresh attempt; genuine native coverage begins at 0. No parallel migration
store or old-schema reader may implement this. Parent's current #229 also permits
canonical plain history only <= H. The full table/caller migration and installed
UNKNOWN/reset proof remain open.

- **Additional closure:** transcript_routes.py now declares TranscriptRoute and
  the existing InputDisplay as sole table owners, deleting initializer JSON/old
  source-column conversion and exclusive compatibility tests. This store is
  durable owner-authored annotation data, not a disposable index. Parent owns
  the precise one-shot contract in evidence/round2-s12/HANDOFF.md.
- **Reply index:** view_unread.py derives ReplyIndex/TranscriptReply, removes
  in-runtime schema conversion, and requires quiet reset of
  transcript_reply_index.sqlite3; durable ReadLedger remains untouched.
- **Still open:** full lifecycle/core/cohort/response table caller closure and
  remaining unowned tables. S12 is not globally complete.
- **Response module closed:** two response tables and join projection now use A13;
  all response mutation callers use declared rows, old SQL roster/mappers deleted.
- **Page index closed:** bus_page_index.sqlite3 uses BusPageSource/BusPageRow;
  A13 typed streaming preserves bounded history and closes cursor resources.
  Runtime quiet reset required. passive_channel_awareness adopts that iterator.
- **Current release contract:** VerifiedOwnerLoss consumes typed release evidence
  with full process identity after parent229/b4cb42a integration. No raw receipt
  fallback; UNKNOWN preserved and only new input eligible after explicit recovery.
- **Goal history closed:** existing GoalHistoryEntry is the sole row owner;
  handwritten schema/JSON codecs/mappers removed, public history shape retained.
  Durable parent one-shot must carry ALL journal states and exact sequences into
  goal_history_entry (contract in HANDOFF), never reset this file.
- **Goal attempt/failure stores closed:** six declared table owners, no runtime
  migration/handwritten schema/row mappers/positional writes. Existing generation,
  reservation/attempt state drives storage; passive evidence excludes capability
  tokens.72 bounded local checks pass. Durable carryover contract in HANDOFF;
  no regrant/replay on reopen, parent owns one-shot conversion.

- **Checkpoint/candidate closure:** private_bus_checkpoint.py and
  wake_candidate_index.py now use declared rows and strict A13 boundaries.
  Old candidate v1 upgrade and converter-only test deleted. Source seals/floor
  and bounded WAL maintenance preserved; reset classifications in HANDOFF.
  Core coordinator/cohort and recovery readers remain open; S12 not complete.

- **Further caller closure:** six cohort table declarations and their readers,
  foreground observer/coverage reads and recovery projection/gateway typed.
  ExecutionAssignmentLink/ConnectivityFacet now sole row and domain owners.
  Remaining: four core lifecycle owners and core raw mutations; parent history
  notification-reader crossing requested. Full237 remains draft, not complete.

## Source closure checkpoint (237)

Foundation230 + caller closure237 now cover every unowned production SQLite table,
mutation/read caller and raw-row mapper. Coordinator schema8 consolidates existing
WakeAssignment/ExecutionRecord/AttemptRecord/ResponseObligation as table owners;
no duplicate flat row class or compatibility mapper remains. Cohort, response,
recovery, checkpoint, candidate, prompt/cursor, goal/todo/transcript/page readers
use A13; notifications use a declared joined projection. The package-wide guard
covers all src modules (A13 alone owns raw SQLite), including the external
read-only OpenCode importer. Removed old lifecycle golden capture/tests.

Full S12 remains **cutover/real-path acceptance pending**, owned by parent229:
quiet runtime reset, durable one-shot preservation, current root access/floor,
no replay of <=H/UNKNOWN and retained-session/native/RPC acceptance. Detailed exact
files/tables and receipts are in evidence/round2-s12/HANDOFF.md. Never mark global
completion from source tests. No runtime installation/reset was performed by S12.
