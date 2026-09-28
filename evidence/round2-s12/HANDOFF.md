# S12 continuation — active, not merge-ready closure

Foundation #230 merged at reviewed cca3b282. Caller closure draft #237. Current branch:
`refactor/round2-s12-caller-closure`.

## Latest caller/table closure

- `CurrentExecutions`, `ReplayAssessments`, `PublicationIntents` are now sole
  behavior and table owners. Deleted CurrentExecutionPointer, ReplayAssessment,
  PublicationIntent classes; no aliases. Updated imports/calls in
  coordination_response.py, publisher.py and wire_log.py. Parent #229 should
  use PublicationIntents if any newly added wire code still names the old type.
- Participants/aliases/generations/pointers and recovery/replay callers use
  typed reads/writes. PublicationReceipt reads its join without a hand mapper.
- `Todo` owns todos.sqlite3 schema and all typed access; no _todo mapper,
  _assignment_token serializer or raw positional inserts remain.
- `tools/cutover/todos.py` stages durable tasks exactly once. It never replaces
  the source or an existing stage. Actual previous main TodoStore -> current
  reopened stage preserved 3 tasks, insertion order, revisions, goal reference,
  current owner, release/transfer retry evidence and done state. Source unchanged.
  Parent runs it at the quiet cutover and deletes it after installation.
- Local receipts: row-owner-closure.log 189 passed/1 skipped;
  declaration-constraints.log 85 passed; todo-durable-cutover.log actual staged
  conversion passed. These are local behavior checks, no installed activation.
- SqlStorage SQLite conversion methods are to_sql/from_sql, leaving the
  DeclaredFamily.decode name lookup intact. The shared new-case SQLite test
  caught and now covers that collision, enum constraints, generated columns,
  autoincrement and ANY's refusal to coerce a stored string into a typed integer.
- Copernicus owns BusRouteCounts and pending/inbox regions; parent owns history,
  required WireAccess/admission_after_seq and #229/0330ad9. Those features are
  preserved on integration; this work adds no metadata owner or fabricated proof.

## Stable NativeRuntimeInput API for Pascal

```python
from agent_comms.native_runtime_input import NativeRuntimeInput
reserved = NativeRuntimeInput.one(db, input_id=input_id)
# reserved is NativeRuntimeInput or None
# reserved.assignment_id
# reserved.sent_owner_admission_generation
```

All other native input fields retain typed current meaning. The derived physical
table is `native_runtime_input`; schema version 4. Current cursor table is
`current_native_cursor`, fields owner_admission_generation and assignment_id.
`history_views.py` (parent) must use native_runtime_input and n.assignment_id.
`selected_tool_broker.py` (Pascal S10) must use the API above. No aliases provided.

## Current local evidence

197 behavior/family/fast-guard checks passed, one skipped. Receipt
closure-prepublish.log. Tests exercise real SQLite and filesystem/process boundaries;
native Pi replies are synthetic here. No provider/installed activation claim.
The first run found one test hardcoding prior schema_version=2; the concurrency
behavior now compares the owner constant. Six fast marked guards passed locally
(closure-guards-initial.log). Full caller closure is still in progress.

## Runtime cutover exact scope

- `coordination.sqlite3`: schema_meta, participants, participant_aliases,
  owner_generations, executions, attempts, current_executions, wake_claims,
  execution_claims, replay_assessments, obligations, publication_intents,
  publication_receipts, connectivity, recovery_audit; native_runtime_schema_meta,
  native_runtime_input, current_native_cursor. Reset the runtime file under the
  parent's quiet lock after durable admission_after_seq is established.
- `native_prompt_bindings.sqlite3`: snapshot_meta, prompt_binding. Parent owns
  reset of the private snapshot and pending intent protocol, preserving UNKNOWN
  as history; this code never repairs/replays uncertain prompts.
- Durable todos are rewritten once with the tool above, never reset. Other
  durable history tables remain to classify and migrate as their callers close.

Parent #229/6fd9857 owns floor/D22. Preserve required admission_after_seq;
acceptance/selection strictly >H, empty native proof 0. Canonical plain history
<=H is legal history, never a new wake. No invented old recipients or parallel
migration store. Installed UNKNOWN reset and durable rewrite proof remain parent
work. This branch performs no live writes/restarts/resets/installation.

---

## Prior foundation receipts

# S12 A13 foundation (surface still open)

No production table or stored data is changed by this foundation. It provides
one declaration owner for table and projection rows; SQLite conversion is owned
by nominal SqlStorage members using FieldCodec, and table membership/names use
DeclaredFamily. Connections and transactions remain with callers.

28 local checks passed (two new family/new-case behavioral tests plus existing
FieldCodec/DeclaredFamily checks). Tests use real SQLite files under the owned
worktree, including close/reopen and rollback. Initial test run failed because
one fixture nested BEGIN after a failed statement and one expected ValueError
where the existing codec raises TypeError for missing dataclass fields; corrected
fixtures preserve rejection and transaction behavior. Both receipts retained.

No mocks, provider calls, live sessions, restarts or installation involved.
Full source guards and affected installed path acceptance remain part of the
consumer migrations; this is not a claim that S12 is completed.

## First production reader adoption

`optional_awareness_projection.py` now declares its join projections and routes
all SQL reads through TypedRow. Removed raw column/tuple extraction and redundant
primitive checks. Scope guard discovers modules importing typed_table, rejecting
raw fetch/row iteration and handwritten table DDL or writes without exceptions.
25 checks pass (A13, guard family and 21 awareness behaviors), Ruff passes.
The shared coordinator schema validators still require sqlite3.Row; the connection
keeps that documented dependency until their own S12 migration. Removing it broke
12 behavior checks; restoring the required connection contract fixed them without
weakening assertions. Failed and corrected receipts retained.

This adoption changes no persisted schema, hence no reset/highwater movement.
A reset of coordination/candidate/native runtime state must occur only at the
parent's quiet cutover: capture the existing checkpoint source highwater H, retain
original history/UNKNOWN evidence, and initialize existing candidate/current owner
admission cursors so all native wake selection is strictly > H. A cursor reset to
zero is not an acceptable install. Parent owns the actual D22 cutover and proof
that old addressed rows cannot create new attempts. No migration store is added.

#230 remains the draft owner for all S12 work. NativeRuntimeInput and table
migrations are still open; foundation adoption alone is not full surface closure.

## Admission-floor handoff (source evidence; parent L0b owns activation)

The existing candidate checkpoint (`wake_candidates.sqlite3`, checkpoint row:
root_id/device/inode/byte_offset/tail_digest/last_seq) records index progress,
NOT eligibility. Rebuilding it indexes historical recipients too. Existing
`cohort_foreground._accept_visible_initials` accepts every addressed initial after
its supplied cursor; `run_foreground_once` starts that cursor at zero.
`SelectedExecution._select` uses `self.after_seq`, also defaulting to zero.
`_production_optional_awareness` and `native_source_cursor._bounded_coverage_pages`
begin their ranges at zero. Therefore a reset plus checkpoint rebuild alone
would make historical inputs eligible again or stall fresh proof coverage.

Concrete proposal for parent review/implementation: persist `admission_after_seq`
on existing WireMetadata during the quiescent D22 rewrite, with H captured under
its existing bus lock and checked against last_seq. Preserve this field through
all future publications/checkpoint updates. This is the current root's authority,
not a migration sidecar. Missing/new-format-invalid authority must fail closed.
All claim acceptance and native selection use max(caller_after_seq, that floor),
including direct accept_initial_cohort calls, not only the foreground scanner.
Optional awareness and bounded native source coverage must start at the same
floor; original messages remain available through history reads. The candidate
index can be rebuilt for complete history and then page strictly above H for
wake eligibility. Checkpoint through_seq/byte_offset still attest the rewritten
source; they are not fabricated native inputs.

Do NOT seed CurrentNativeCursor.covered_seq or injected_seq to H: those are native
proof projections. Initial native proof is empty; only post-floor verified input
may advance it. Admission floor does not assert that old UNKNOWN work succeeded.
Required cutover test: retain an old addressed unfinished/UNKNOWN input <= H;
reset and relaunch; verify zero new claim/native/provider attempts for it, then
append one addressed input > H and verify its single normal admission. Repeat
ordinary restart/index rebuild to prove the floor survives; history retains both.
This is an explicit remaining integration requirement, not completed behavior.

Current source diff: -105/+503 lines. Tests: -0/+176 lines. Additions establish
A13 and typed join shapes; existing behavior tests were not ported or weakened.


## S12 durable transcript annotation cutover contract

`transcript_routes.sqlite3` and any saved `transcript_routes.json` contain owner-authored data. NEVER reset them as transcript indexes. Parent owns the one-shot tool under tools/cutover and quiet installation; no runtime converter remains in #237.

New declarations in transcript_routes.py: `TranscriptRoute(session_file, entry_id, routing: TurnRouting)`, physical `transcript_route`; existing `InputDisplay` is the sole display/binding row (`native_id`, `text`, `routing: TurnRouting|None`, `sent_text_digest: str|None`), physical `input_display`. All schemas/writes/reads derive from A13. `TranscriptRoutingStorage` uses the existing TranscriptCodec (Message boundary retained), selected by the routing field, not a second serializer. `TranscriptRoutes.input_bindings()` now returns dict[str, InputDisplay], and its sole current consumer compares typed digest/routing facts.

One-shot input inventory must preserve:
- routes rows keyed by exact session_file/entry_id, including paths for archived sessions;
- display_text, including NULL (internal) versus missing row versus empty text;
- all input_routing digest/routing rows; reject/report orphan input_routing without input_display instead of silently discarding it or manufacturing human display;
- optional JSON source and source-column precedence: current indexed SQLite entries win; in the retired source-column shape, JSON replaces only source='legacy' rows and supplies absent keys; in the source-free current predecessor, SQLite wins. If metadata routes_imported exists, JSON was retired and must not overwrite current SQLite. legacy_revision/routes_imported metadata and source column do not enter the new store.

Read old stores read-only under the parent's quiet lock, decode existing routing with TranscriptCodec, create a NEW exclusive stage via current TranscriptRoutes/typed rows. Preserve each native ID, digest, text and routing semantically, plus per-session entry keys; verify counts and reopened paged input/reply attribution. Reject invalid/conflicting rows with source identity; leave originals unchanged. No installation on partial success. Parent installs only after full verification, then retires the old JSON/schema and deletes the one-shot tool.

Runtime/derived `transcript_reply_index.sqlite3` is separately resettable; new declared ReplyIndex/TranscriptReply tables replace the prior schema. `ReadLedger.filename` is durable human read position and MUST remain intact. No source data is deleted here.

### Current local closure evidence

- durable-route-closure-final.log: 38 passed, including current native transcript/page
  attribution, bounded 10,000-entry route paging, immutable input binding, durable
  incomplete-schema refusal without writes, reply/unread behavior and A13 guards.
- native-consumers-bounded.log: 88 passed, 1 deselected. Large certificate fixture
  construction (>8MiB and 1001 fsynced publications) exceeded a prior bounded run;
  that test is not counted green. UNKNOWN tests use real subprocess/filesystem
  boundaries; replies are test fixtures, not installed Pi/provider acceptance.
- Removed old JSON/source-column importer, migration_source/routes_imported runtime
  paths, split input_routing schema, and four exclusive compatibility tests.
- Current parent b4cb42a has OwnerReleaseStore but VerifiedOwnerLoss still calls
  removed _read_owner_release_receipts. S12 owns the typed current caller fix;
  no old receipt fallback. Integrate parent process-identity/release declarations
  before validating that fix.

## Parent b4cb42a integration and current response/page closure

Parent229/b4cb42a integrated without conflicts, preserving access/admission floor,
current history validation and parent one-shot tools. Changes below are S12;
inherited parent/Darwin/Pascal/Copernicus changes retain their original owners.

- VerifiedOwnerLoss now reads the typed OwnerReleaseStore; no removed raw reader,
  nested thread JSON decoding or PID-only liveness. Require the released/current
  exact process identity, then identity-bound death. Prior admitted generations
  can still settle after a later attested release of the same incarnation. Unsent
  UNKNOWN requires exact stopped generation; source input/proof stays unchanged.
  35 checks pass including real child lifetime, PID/start-time mismatch refusal,
  later release and UNKNOWN abandonment followed only by new input.
- coordination_response.py fully adopts A13: ResponseSchemaMeta (version2),
  PublicationAppendDispatches and SelectedResponseRoute projection. Deleted
  response DDL roster, raw fetches, positional writes and handwritten updates.
  All terminal obligation/attempt/execution/assignment/pointer writes use their
  row declarations. Existing transaction/dispatch no-resend fences remain.
- Runtime reset: coordination.sqlite3 additionally includes response_schema_meta
  and publication_append_dispatches; same whole-file quiet reset, not migration.
- BusPageSource and BusPageRow derive the bus page index schema (physical names
  bus_page_source and bus_page). Typed offsets STREAM, close their cursors, and
  validate only consumed rows; history never materializes the whole index.
  bus_page_index.sqlite3 is derived/resettable at quiet cutover. Current cache
  damage still uses authoritative bus rows; old schemas are not interpreted.
  MessageBus history needs no edits; passive_channel_awareness._exact adopts the
  typed iterator with next(), retaining exact-source checks and no wake rebuild.
- SQLiteSchemaObject/SQLiteForeignKeys are shared A13 projections; deleted copies
  from sidecar/native schema modules rather than adding more metadata mirrors.
- 24 page/response/admission-floor/A13 checks pass (pages-response-floor-focused),
  including real DB reset/reopen/index rebuild excluding rows <= admission floor.
  Broad passive-awareness testing is blocked on Darwin's current registration
  source: ThreadManagement.claim_thread still passes removed pid argument.
  Reported concretely on235; no registration compatibility added here.

## Durable goal history table closure and parent tool contract

`goal_history.sqlite3` is durable, NEVER reset. Existing GoalHistoryEntry now
owns its table/schema/index, typed Goal before/after values, owner_created_at,
and pending/committed/aborted/uncertain state. Old entries/metadata DDL, _encode,
_decode and positional row reconstruction are deleted. Its public to_wire keeps
exact original five fields and nested Goal state tags, excluding internal owner
and journal state.12 behavior/family/guard checks pass, including crash before
registry write, lost commit ACK, fsync uncertainty, reopen/rename and observed gaps.

Parent tool `tools/cutover/registry_history.py` currently rewrites JSON inside
retired `entries`; it must instead create a NEW current GoalHistoryStore stage
and insert typed GoalHistoryEntry rows, preserving ALL source rows (including
pending/aborted/uncertain), exact sequence, owner_created_at, kind, state,
observed_at, and converted before_goal/after_goal as before/after. Constructors:
`GoalHistoryEntry(sequence, kind, observed_at, before, after,
                  owner_created_at=..., state=...).insert(db)`.
Physical table is `goal_history_entry`; schema comes from its declaration. Use
existing StoredGoal.current for snapshot rewrite; no new runtime reader. Preserve
source backup, no resequencing/re-timestamping or observe()/automatic reconciliation
while staging. Compare all rows after close/reopen; do not install old metadata.
This extends parent's existing one-shot, not a second tool or migration store.

## Goal attempt ledger closure (durable evidence, no automatic regrant)

Six tables now derive from GoalLedgerTable capabilities: Generation, AttemptRecord,
GoalHumanDecision, GoalProviderUsage, GoalAttemptSchema(version6), FailedTurnEvidence.
Generation and AttemptRecord are the existing semantic owners; lifecycle/phase
and Reservation are stored through FieldCodec, not reconstructed from flat rows.
AttemptRecord's query keys derive as generated columns from Reservation; no
second token/goal/generation authority is written. FailedTurnObservation keeps
its reservation capability only in memory and persists separate non-secret
FailedTurnEvidence after exact reservation/evidence identity agreement.

Deleted runtime v2/v3/v4 migration, old DDL creators, positional writes, hand
row mappers and three exclusive converter tests plus per-table mapper tests.
72 behavior/family/guard checks pass,8ACP cases excluded until current parent
registration/test fixture integration. Actual process crashes, concurrency,
commit/fsync uncertainty, explicit retry and secret-free passive evidence tested.

`goal-private/goal_attempts.sqlite3` must preserve evidence, not be blindly reset:
goal generations/UNKNOWN attempts, human decision IDs and exact provider usage
are durable. Parent owns the one-shot, no duplicate converter created here.
Carry old goals to Generation(goal_id,number,typed GenerationState,attempt_id,
ready_digest=...), attempts to AttemptRecord(Reservation(goal_id,generation,
attempt_id,token),typed GoalAttemptPhase,progress_witness,resolution), all human
decisions to GoalHumanDecision, exact canonical usage_json to GoalProviderUsage,
and existing observations to FailedTurnEvidence. Preserve every token/digest,
ID/generation, phase/resolution/witness and usage value; no backfilled observations.
Create a fresh current schema via GoalAttemptStore.initialize(stage0700), then
insert parent rows before their references and reopen for equality. Do NOT call
create_goal/reserve/claim/authorize APIs when staging; those would mint authority.
Reopened stores intentionally have empty in-memory _ready_grants/_owned; no
previous READY or RESERVED/CLAIMED row is launch permission. UNKNOWN stays
unresolved, explicit existing owner decisions still required. Parent must keep
archived evidence without allowing old rows to bypass the durable admission floor.

### Parent121f526 integration receipt

Latest parent integrated without conflicts. Goal attempts, failure observations,
goal history and lifecycle:82passed including the previously excluded ACP cases.
ACP fixtures now explicitly configure the current private root and native package.
These are real SQLite/process crash/reopen tests and controlled ACP backend tests,
not an installed provider activation claim. Parent owns quiet installation.

Inherited passive-channel suite still expects session startup to initialize the
optional JSON awareness ledger; current parent has no initialize caller. Observed
43passed2failed in combined diagnostic, first failures are missing ledger. No
production fallback or invented awareness has been added. Parent channel owner
should close that old caller/test boundary deliberately.

## Certified checkpoint and candidate index closure

PrefixCertificate now owns its SQLite schema via A13 (version2, physical
`prefix_certificate`); ResponseKeys, Initials, Addressed own the other sealed
source tables. Deleted the local DDL/insert/update generator, table-name roster
and all raw readers. Existing writer-owned pending/final seal fsync ordering,
complete canonical prefix verification, exact source IDs and bounded pages stay
in place. Typed schemas/reads reject corruption; retained validated history below
parent floor still reaches _index_row without a duplicate rejection.

Existing Candidate is now its table owner (physical `candidate`), with derived
selected/passive indexes; CandidateCheckpoint(version3, `candidate_checkpoint`)
and CandidateResponseKey(`candidate_response_key`) own the remaining projection.
Deleted tuple row alias, positional writes, raw checkpoint/page decoders and
allow_v1_rebuild runtime upgrade. An incompatible/partial schema is refused
without modifying saved rows even on rebuild=True; parent resets disposable
old format outside src. Current-format explicit rebuild still replays a bounded
source batch, never runs in send/wake and never grants source/native authority.

Reset classification: wake_candidates.sqlite3 (+ SQLite WAL/SHM after quiet
close) is disposable. private_bus_checkpoint.sqlite3 is a derived source index
whose inode/content is sealed in durable WireMetadata: parent must remove/reset
checkpoint_version/checkpoint_seal together with that sidecar, then certify the
current canonical preserved bus using install_private_bus_checkpoint. Preserve
last_seq, root ID, admission_after_seq and access exactly; never merely delete
the sealed sidecar while leaving its old marker binding, nor seed native proof.
Archived snapshots require the same current source schema/marker consistency.

84 focused behavior checks passed, then18 cursor/consumer/guard checks passed.
Tests include real on-disk rebuild/reopen, duplicate response keys across batches,
WAL readers during writer commits, corrupt/missing index denial, fsync uncertainty,
complete native-source page barriers. Four large scale cases excluded from the
consumer run to keep bounded; no installed provider activation claim. Updated
current marker fixtures to required floor/access and actual WireLog fsync seam.

SQLiteJournalMode moved from owned goal ledger into A13 for reuse by candidate
and cohort connections; S9's JournalMode/JournalSchemaObject can adopt shared
SQLiteJournalMode/SQLiteSchemaObject at its coordinated crossing (not edited here).

## Cohort, recovery, and relation owner closure

Coordinator schema4 consolidates ExecutionAssignmentLink and ConnectivityFacet
with their row declarations and deletes ExecutionClaims/Connectivity duplicates.
Physical execution_claims now uses assignment_id; external snapshot claim_id
spelling stays unchanged. Connectivity fields are owner/acp_client, typed enums.
Old positional relation/connectivity writes and snapshot mappers deleted.
Core WakeAssignment/ExecutionRecord/AttemptRecord/ResponseObligation lifecycle
consolidation and their remaining mutation calls still open.

All six cohort tables now own A13 schemas, exact checks, foreign keys, sealing
and immutable-fact triggers. Cohort metadata and optional provenance version2.
The recipient/sequence index derives from CohortDeliveryReceipts. Deleted two
handwritten DDL/name rosters and redundant cohort validation copy; the sole
assert_cohort_schema lives in cohort_schema, current imports updated. Mandatory
installation and optional savepoints preserve omission/no-retroactive-proof
semantics. Parent's strict admission_after_seq floor remains at acceptance and
foreground scanning, and native proof is never manufactured from that floor.
Cohort acceptance/receipts/pages, foreground observer projection and coverage
reader now use typed SQL boundaries and writes; no raw row extraction remains
in these modules. Existing _assignment conversion accepts typed WakeClaims
while core nominal lifecycle consolidation remains open.

Recovery readers/gateway now use one declared joined RecoverySelection and
existing public ProjectedRecovery/ProjectedConnectivity types. Deleted raw
integer/boolean checking and positional mappers. Removed obsolete forced
execution_owner_status_idx references: the declared owner/status index serves
those bounded queries. Existing canonical owner scope, Linux peer credentials,
read-only rollback transaction, reply redaction and bounded child cleanup stay.
SQLiteUserVersion is shared A13 authority; removed three identical owner-local
classes in coordinator, reply index and awareness.

Evidence: recovery/socket45pass plus one removed-helper-only failure; after
deleting the helper assertions the retained actual corrupt-view test+family
guards6pass. Coordinator/recovery/awareness160pass (one96child stress excluded).
Cohort first25pass2stale parent-root expectations; consumer40pass2issues
(archived-target message expectation, fake provider lacking actual bounded
pre-admission wait); corrected only these seams, final7pass including actual
concurrent DB acceptance through the existing raw writer's admission wait.
No unchanged suite repeated afterward. Real OS socket/SQLite/child checks are
provider-free, not an installed native activation receipt. Parent owns that.

Parent-owned crossing requested before integrating this batch: history_views
notification join must produce typed WakeClaims before _assignment, or parent
can hand off that exact reader. Its current raw join also retains retired
native_runtime_inputs/n.claim_id names. Parent owns history changes; no file
edit made here, no compatibility fallback. Full237 remains draft until remaining
core lifecycle/callers and this crossing close.

All cohort/recovery/relation tables are inside coordination.sqlite3 and reset
under parent's quiet D22 procedure, preserving WireMetadata admission_after_seq
and access/current history. Recovery projection/gateway add no durable store.

## Response obligation owner closure

ResponseObligation now directly owns the obligations table and its lifecycle;
Obligations and the snapshot mapper are deleted. Coordinator schema5 stores
ResponseState through FieldCodec and derives state/receipt query columns. All
obligation mutation callers write the typed lifecycle. Existing snapshot and
external publication formats remain unchanged. BEFORE triggers read the actual
NEW/OLD lifecycle JSON: SQLite can expose unset NEW generated columns during
metadata-only updates. The real constraint tests caught this and now prove the
immutable receipt and same-state metadata guards still reject invalid writes.
186 focused coordinator/response/recovery/awareness/guard checks passed (one
concurrent fresh-process stress test deselected). No installed activation claim.
Runtime reset remains the entire coordination.sqlite3 under parent's quiet
cutover; no new durable store. ExecutionRecord/AttemptRecord/WakeAssignment
consolidation and the parent notification reader crossing remain open.

## Execution and attempt owner closure

Integrated parent720316a and notification241 at92b42ba before continuing.
ExecutionRecord and AttemptRecord now own executions/attempts; removed Executions,
Attempts, _execution, _attempt and generic _row. Typed lifecycle writes cover
creation, retry, lease renewal, progress/finality, response settlement and verified
owner loss. Lifecycle-derived generated query/FK columns preserve actual SQL
constraints; BEFORE triggers use base lifecycle expressions, including declared
transition edges. Coordinator schema7. Both remain runtime-only coordination.sqlite3.
Current snapshot names remain projected; generated SQL columns cannot be supplied
as constructor state. No compatibility readers or aliases.

Execution acceptance:196pass with one stale constructor-field assertion corrected.
Attempt acceptance:84 coordinator constraint cases passed, then85 changed store,
response, nominal, recovery and family guard cases passed after correcting the
optional no-attempt lookup. Raw SQL fixtures now write current lifecycle JSON;
actual corruption and immutable-identity/finality tests retained. No repeated
unchanged gateway suite. Remaining production consolidation: WakeAssignment and
its notification/cohort/caller boundary, then global guards and final integration.

## S12 source closure: wake owner, notifications, package-wide guard

WakeAssignment now owns wake_claims (physical assignment_id, lifecycle).
Deleted WakeClaims and _assignment; all coordinator/cohort/publication/native
triage writers use declared columns. Assignment/engagement classes own SQL
mode/verdict/binding projections; removed the old hand-listed assignment case
constraint. Generated mode/verdict/binding columns remain read-only. Schema8
preserves acceptance immutability, exact execution relations, transitions and
CAS. NativeRuntimeInput/ExecutionAssignmentLink/cohort FKs and history/awareness
joins all target assignment_id. External snapshots still spell claim_id.

HistoryViews notification query uses NotificationAssignment(TypedRow), preserving
241's process_alive/current-turn checks. Its actual current schema query no longer
passes a raw joined SQLite row into a deleted mapper. No history/attachment data
regions were changed. OpenCode's external SQLite snapshot importer now uses typed
read-only projections and streams messages with explicit cursor release; no
foreign format rewrite or source writes. This was the final raw SQL reader.

The guard now covers every Python file under src/agent_comms recursively; only
its owning typed_table implementation can create tables/write column lists/read
raw SQLite. No adopted-module loophole, per-module allowlist or runtime converter.
All source extraction/write/DDL checks pass. Removed the lifecycle old-format
capture-only test and evidence/s3/legacy-lifecycles.json. New-case behavior proves
a declared assignment gets SQLite mode/binding projection and transitions without
editing a registry/schema roster. Existing no-replay and fault tests remain.

Acceptance receipts: wake-owner-current134pass,1stress deselected; SQL boundary
first13pass including real external OpenCode readonly file equality; remaining
native consumers68pass after current S9 fixture corrections; two previously failing
consumer cases2pass. Native checks include actual child release/UNKNOWN recovery,
current proof/digest mismatch, no wake replay, pointer/revocation, cohort-page
continuation, notifications, and shared declarations/guards. Some providers are
fixture-controlled; this is not an installed retained-session/RPC activation proof.
That real-path acceptance remains parent-owned. No unchanged suite rerun after
these results. Focused lint (excluding existing long SQL lines) and diff checks pass.

### Exact remaining cutover obligations (parent)

No unowned S12 production table/caller remains. Source closure is ready for review;
S12 overall is NOT globally complete until quiet activation/durable one-shots and
real retained-session/native/RPC acceptance finish. No live operation performed.

- Close owners before resetting coordination.sqlite3 and its -wal/-shm together.
  Schema8 includes core typed lifecycle tables, cohort/awareness, response metadata,
  native runtime input/current cursor; history/backlog never becomes fresh work.
- Reset native_prompt_bindings.sqlite3 and closed SQLite sidecars; no manufactured
  current proof or binding, no reissued UNKNOWN/input attempt.
- Derived wake_candidates.sqlite3 and transcript_reply_index.sqlite3/page index
  files may be recreated only after current canonical source validation. Candidate
  checkpoint is an index boundary, not input/provider authority.
- For private checkpoint sidecar reset, remove checkpoint_version/checkpoint_seal
  from the SAME staged root marker and recertify the canonical full prefix before
  attach. Preserve root ID, last sequence, required access and admission_after_seq.
  Capture H once under quiet lock; current admission/scanners stay strictly >H;
  native proof starts0 until genuine post-floor source coverage. No parallel store.
- Durable todos.sqlite3, goal history + goal-private/goal_attempts.sqlite3,
  transcript route/input-display annotations, read ledger and attached history
  preserve IDs, rows, content and uncertainty through parent's one-shot stage and
  reopen equality. Never reset them with runtime state. Parent already implements
  actual annotation and goal conversion; do not duplicate converters in src.

S9 owns compaction journal tables/adoption; its remaining JournalMode and
JournalSchemaObject duplicates should use A13 SQLiteJournalMode/SQLiteSchemaObject.
Parent was notified; this file was not edited. S10 owns selected_tool_broker and
already uses NativeRuntimeInput. Parent retains actual quiet activation/unknown
session proof and deletes one-shot tools after successful durable replacement.
