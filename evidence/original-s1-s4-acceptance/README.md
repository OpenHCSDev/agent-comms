# Original S1–S4 maintained acceptance closure

Original requirements read from `plans/nominal_refactor/files.zip` (S1–S4,
acceptance and migration clauses); audit281 qualification checked against current
owners and maintained tests. No production, native_pi, coordination owner or Toad
App edits. Wegener owns native lifecycle288; parent owns released Toad caller fix.

## Changes and deletion

- `test_continued_private_session`: replace the obsolete ownerName/ownerPid/
  ingressKey/reservedRevision shape with existing `admission_identity`,
  SelectedAdmissionSource and FieldCodec. Preserve all retained-history, unknown,
  mismatched-content, raw-context and no-replay assertions. Refresh only the
  typed reserved revision through the existing shared source fixture helper.
- `test_coordinated_runtime`: migrate four obsolete partial/raw source fixtures
  (fresh enrollment, unknown fsync, persisted reservation, raw prewrite failure)
  to existing `manual_source`, including the registered incarnation where known.
  These tests now reach the intended durable-input gates rather than failing
  incidentally on a retired record format. No aliases or compatibility decoder.
- `test_read_ledger`: extend randomized read correctness with DM paints,
  partial channel paints, cached/stale proofs, participant deletion/rebind,
  mode changes and real child abrupt-exit/reopen operations in each seeded run.
  Separate painted facts and acknowledged facts; compare actual durable reads
  after every operation against current participant incarnations. Existing
  bounded-page, sparse-ack, replacement-bus and crash/rebind checks remain.
- `test_coordination_nominal`: pin the three original rules whose diagnostics
  disappeared as genuinely current variant constraints: no frozen target on an
  unstarted assignment; complete execution/target binding on engagement; complete
  receipt only on a publication. Decode through canonical FieldCodec, including
  missing/extra-field rejection; no legacy golden or retired state table.

## Requirement-to-maintained-test mapping

| Original requirement | Current maintained acceptance / qualification |
| --- | --- |
| S1 new event case, inherited reactions, dispatch guard | test_agent_events: ContextWarning reaches actual turn consumer, MRO override/order and frozen MI payloads; no raw event case reads in owned_turn/turn_progress. |
| S1 correlated model/thinking results | test_agent_events request-correlation separation, late/cancelled result tests; same PendingRequests owner, no duplicate map. |
| S1 shared settlement including publication failure / waiter release | test_agent_events settlement tests and test_backend_settlement: real turn lease/store sequencing, old-turn replacement preserved and continuation survives native settlement. |
| S1 real old/new streams through both original consumers | Historical differential replay remains unproven. Original Participant/agent_loop consumer is deleted; maintained turn/ACP consumers are the current owners. Do not resurrect a second consumer or claim old/new equality from current tests. |
| S2 new phase/failure and consistent pairwise precedence | test_pi_rpc_nominal new excursion plus 36 pairwise failure cases; declared text/code/input uncertainty agree; pending out-of-order/anonymous correlations and malformed/unknown boundary cases. |
| S2 native compaction failure / child disconnect | test_selected_summary_failure_native actual immutable Pi RPC + localhost HTTP, durable selected-summary journal, get_state/reopen and unchanged retained history. Three cases pass; no paid provider, no mocked native child. |
| S2 exhaustive original old/new transcript matrix | Full original matrix (including stall/steering/provider and summarization retries) remains historical qualification. Existing current tests do not certify exact differential equivalence to a deleted implementation. |
| S3 exact original legality accounting | Original f9854ab has exactly47 raises across WakeClaim5, ExecutionRecord8, ReplayAssessment3, ResponseObligation6, RecoverySnapshot25. original-legality.json records each requirement and current named validator owner:44 retain the diagnostic, three are the typed constraints tested above. This is source/owner accounting plus current behavior evidence, not a historical predicate-equivalence proof. |
| S3 declarations, current durable phase tracking and real gateway | test_coordination_nominal test-only response/execution/assignment extensions, SQLite store/socket roundtrips and actual phase observations; test_coordination + test_coordination_store exercise current coupled fences, response publication, retry/ambiguity, crash/reopen and concurrency. |
| S3 old value golden / old transition-table equality | Old internal formats and compatibility fixtures are superseded by current-only owner instructions. No retired table or reader restored. Current external gateway/declared-field checks retained. |
| S4 randomized display/read/mode/rebind/crash property | Existing test_shown_only_property plus new test_read_property_includes_dm_rebind_stale_paints_and_abrupt_reopen, seeds7/31/99. Sparse membership and incarnation-sensitive reads checked after every operation. Scope is painted-page ACK; deliberate human Mark Read is a distinct existing operation. |
| S4 known characterization and new view mode | test_read_ledger existing any-mode hidden-message, bounded-page, process-exit/rebind, bus replacement, partial-paint and AlternateMessages tests. |
| S4 retired scalar marker conversion | Durable reset/rewrite was already authorized/deployed and converters deleted. No compatibility/migration fixtures reinstated. |
| S4 installed Toad calls | Earlier287 menu used the older paired Toad wheel, not released main. Exact installed source matches09b8a507, receipt previous-menu-provenance.json. Parent completed released-main thread_actions migration in Toad132 b4bdae1 and deployed paired core4510; this PR does not redo that fix or claim a new Toad-launch check. |

## Verification

- baseline.log:163 passed,2 skipped (selected_summary_exchange opt-in larger
  heartbeat cases); existing S1/S2 nominal checks, S3 gateway and S4 properties.
- final-source.log:223 passed, including three actual native selected-summary
  failure/disconnect cases, migrated history/runtime callers and full current
  coordination/coordination_store suites. No CI wait.
- properties-final.log and installed-final.log record final strengthened checks
  separately. Installed tests import the noneditable candidate package; test
  modules remain the maintained source tests.
- No broad NRA scan rerun for test-only changes. Parent's current complete-package
  scan remains the structural receipt; this mapping is manual semantic ownership.

Universal size/benchmark claims and historical differential equivalence remain
qualified, not newly claimed complete. Retained/live history, configured provider
transport and parent deployment are untouched.

## Wegener baseline29 closure

Exact baseline node IDs and original receipts are preserved in wegener-handoff.json
(from PR287 comment5880931529). These failures predated PR288. Current tests now:

- Decode ACP metadata with existing decode_updates, CursorAdvancedUpdate,
  CursorEnvelope and InputDeliveryChangedUpdate; retain scope, admission, delayed
  old-owner race, real lock contention and no-model delivery assertions.
- Inspect canonical ReservedInput and NotSentInput rather than the deleted scalar
  inputDisposition projection. Eight refused handoffs assert not_sent, no native
  binding/start, retained unresolved notice and refusal to bind a retry. Fresh
  input, controller/image retention and unknown no-replay checks remain.
- Run the selected-file preflight against the actual pinned native package and an
  actually uninitialized root; retain refusal before registration or file mutation.
- Assert explicit STOP leaves no executions/attempts in the canonical store, whose
  creation now belongs to registration, rather than assuming the file is absent.

Deleted four copied cursor-v1 reducer tests and the 247-line retired JSON fixture.
They simulated a removed projection instead of exercising the maintained owner.
Backend scope/race tests remain and decode the canonical typed extension. The
paired mounted consumer migration was separately completed by parent Toad132.

wegener-final.log records 45 passes and one missing-import test failure, corrected
before the final installed run; it is not a green receipt. wegener-installed.log
records 46 passed in 35.50s against the installed package, with actual pinned Pi child/local HTTP
fresh-input lifetime acceptance enabled (no paid provider, no mocked child).

## Current main295 integration

Merged main e6fa8feb (including parent295 trusted-load recovery) without conflicts.
current295-cursor.log:25 passed in15.59s against current source, including the
parent real file-lock/RuntimeServer TCP recovery test and all24 maintained private
ACP delivery checks. Parent recovery test is retained unchanged and distinct from
the deleted cursor-v1 fixture reducers. Prior46 installed/native checks above
precede this main sync; this focused receipt does not claim a new deployment.
