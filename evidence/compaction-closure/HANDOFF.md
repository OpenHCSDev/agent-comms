# D2–D4 compaction closure — Darwin

Source commit052581f; reconciled with main1836d2ee02 in bf701fd. Persistent branch `codex/compaction-lifecycle-closure-20260928`, tree `/home/ts/wt/comms-compaction-lifecycle-closure-20260928`. Parent owns integration/deployment. D1 body unchanged by this branch; its merged implementation is included from main. Draft PR URL added after publication below.

## Complete owned replacement

Implemented in dependency order from the accepted post-PR95 decision:

1. **D2 journal lifecycles.** New `compaction_states.py` extends existing DeclaredFamily/LifecycleState. OperationState, SummaryState and PublicationState are independent families. Successors, terminality, original eligibility, selected native-commit eligibility and native-result checks live on their declarations. Journal SQL CHECK/partial-index/query choices derive from those declarations; SQLite rows decode once when loaded. LinkedSummary owns its commit ID; DeclinedPrestartSummary owns its reason. Returned admission ACK scope now binds the typed terminal state; its exact weak identity and post-COMMIT parent fsync issuance remain essential. Merely loading a terminal row cannot mint a capability. Linked original admission checks belong to LinkedSummary rather than a status switch in SelectedSummaryAdmission.
2. **D3 native witness.** NativeWitness is one immutable FieldCodec record, declared on the existing preparation boundary. Preparation, source capture, selected probe/summary RPC, owner runtime, commit/admission/reconciliation and current fixtures use its fields. NativeOutcome decodes native status once, delegating receipt/metadata checks to the operation declaration. CompactionSource keeps its typed witness; its journal projection retains the exact canonical `native_json` string. Independent native CAS, owner/ingress/source rechecks and their locks are retained.
3. **D4 detached provider closure.** Deleted unused `summarize_native`, embedded `_SUMMARIZE`, its two transport-only tests and now-unreferenced NativeSummaryError. Deleted str outcome adapter/annotations; every current synthetic callback returns NativeSummary or another OwnerSummaryOutcome. Deleted duplicate PreparedOwnerSummary and NativePreparation.session_id storage; callback receives NativePreparation directly and uses witness.session_id. Kept the actual selected child RPC, NativeSummary/outcome behavior and shared native-usage validation.

Other deleted surfaces: Outcome Literal, _TERMINAL, hand-maintained SQL status rosters, raw journal status fields, SelectedSummaryAttempt's parallel commit/reason fields, SelectedSummaryAdmission's parallel _status/_commit_id, witness dict copies/key rosters, native outcome status-to-fields dispatch. No old aliases, compatibility constructors, new store or shared-self facade. Empty RefusedOperation uses inherited terminal behavior; TerminalOperation is a stateless implementation mixin, not a registry or mutable authority.

Production files: compaction_states.py (new), compaction_journal.py, owner_compaction_prepare.py, owner_compaction_commit.py, owner_compaction_runtime.py, owner_compaction_adaptive.py, owner_compaction_provider.py, selected_pi_route.py, selected_pi_summary_rpc.py, selected_summary_admission.py. No MessageBus, manual transaction, native package, claims or pins edited. Current paired Toad src/tests search found no removed compaction API consumer.

## Current internal contracts for parent harnesses

- `operation.state` replaces `operation.status`; `.state.committed` / `.state.terminal` govern behavior, `.state.declared_name` is stored spelling for evidence.
- `attempt.state` replaces status/commit_id/decline_reason fields; LinkedSummary.commit_id and DeclinedPrestartSummary.decline_reason own terminal data.
- `journal.resolve(id, OperationState_instance, evidence, publication=...)`; no string adapter.
- `FieldCodec.decode(NativeWitness, external_dict)` exactly once on external fixture/native input. Internal `capture_source`, `commit`, `probe_idle_selected_pi`, `run_selected_summary` require that type. Native JSON remains camelCase and unchanged.
- `NativePreparation(witness, tokens_before, is_split_turn)` goes directly to typed summary callbacks; session identity is `preparation.witness.session_id`.
- `_call` returns NativeOutcome; transport interceptors inspect `.state` / `.evidence`, and represent uncertainty with `NativeOutcome.unknown(reason)`.
- CompactionSource journal view: `FieldCodec.project(source, "journal")`. Old persisted `native_json` is preserved; no internal serialized-witness accessor remains.
- `_from_returned_ack` is private journal issuance, now carries terminal state rather than string/id pairs. It still requires the exact returned fsync receipt; no API to reconstruct one.

## Focused local evidence

All commands run in this owned tree with `PYTHONPATH="$PWD/src"`, existing interpreter `/home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python`, `-m pytest -o addopts=''` (xdist disabled) and60s shell bounds unless noted. Basetemps under owned `.closure-work`; native queue uses short persistent `/home/ts/wt/.d234q` to avoid Unix socket path limits. No paid/provider acceptance rerun.

- **journal-verified.txt:80 passed**7.68s. test_compaction_journal, test_selected_summary_journal, test_selected_summary_admission, test_compaction_send_admission. Immutable terminal state; UNKNOWN cannot become pre-write refusal; reservation/source/once-only ACK; rollback/lost parent fsync; raw prewrite exclusion and no row-derived grant.
- **consumer.txt:69 passed,2 skipped**20.24s. test_owner_compaction_runtime, test_selected_pi_route, test_selected_summary_exchange, test_selected_summary_guardian_combined, test_selected_pi_summary_rpc, test_compaction_publication, test_input_drain. Two native opt-in exchange cases unconfigured in this batch. Cancellation joins native worker; child uncertainty retires child;402/timeout detail preserved; metadata publication; owner/original/steer/shutdown/UNKNOWN/future queue fences.
- **boundaries.txt:10 passed**0.19s. New test_compaction_boundaries: literal original SQLite schema/rows reopened without rewriting source/evidence or dropping terminal/UNKNOWN history; incomplete forged terminal record rejected; family spelling/transitions; malformed/extra/missing witness fields; exact source/native JSON; mismatched native metadata becomes UNKNOWN.
- **native-first.txt:57 passed,1 failed**149.64s,165s shell bound. test_owner_compaction_prepare, test_owner_compaction_adaptive, test_owner_compaction_commit with the current prepared bundle below. Real native prepare/CAS/file metadata/UNKNOWN reconciliation/owner ingress/parent SIGKILL/watchdog exclusion ran. Failure was one remaining f-string synthetic summary callback in the three-round ACP metadata test; old string API had been deleted. Migrated callback to NativeSummary, not a compatibility fallback.
- **native-closure.txt:13 passed**26.54s. Corrected three-round ACP/native metadata test + adaptive tests + cancellation/runtime cases after final NativePreparation callback closure. Resolves the native-first failure. Also covers current direct typed preparation callbacks.
- **native-queue.txt:4 passed**23.88s. test_input_drain_native (both foreign=False/True) + selected summary commits/admission once + owner-without-goal. Real prepared native SDK host/local injected provider: durably accepted future queue waits through selected summary/native commit, distinct original/followup each run once, unrelated foreign UNKNOWN retained, current one-use original admission. Not a configured-provider acceptance claim.
- **continued.txt:63 passed,4 skipped**2.76s. New boundaries + fresh/continued private session history + selected exchange. Covers migrated journal callers/old coverage. Four optional native cases not configured in this batch.
- **merged.txt:19 passed**1.33s after main183 merge. Boundaries + D1 manual owner + owner runtime. D1 body unchanged; no additional provider acceptance.
- Ruff I/F over all28 changed Python modules and git diff --check passed. Parsed all28 modules. Caller search retained in callers.txt.

Native commands set `PI_COMPACTION_TEST_PACKAGE=/home/ts/wt/comms-refactor-integration-20260928/stack/.pi-native-aef88838db0496c2/node_modules/@earendil-works/pi-coding-agent`. Read-only reuse of parent's prepared bundle; disposable sessions/private data only in owned fixture roots. No native bundle copy, mutation or provider network.

Earlier failures retained: journal-first7fail/73pass (old fixture signatures and malformed artificial terminal rows); journal.txt1fail/79pass (terminal-state lookup incorrectly instantiated LinkedSummary without commit ID; fixed to decode all state columns); consumer-first55pass2skip14fixture errors (placeholder `native-revision` migrated to valid typed revision). No remaining failing tested behavior. Counts above overlap; do not sum them as distinct coverage. Optional skipped suites are not claimed green.

## NRA coverage and proof limits

nra-before.json/nra-after.json: exact_compact_global,79analyzed,0omitted,complete,0findings. Whole src context; report targets journal/new states, preparation/commit/runtime/provider, selected admission/RPC. Existing NRA interpreter `/home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python`;60s shell bound; `-m nominal_refactor_advisor --json --json-payload summary --parse-workers 2 --analysis-workers 2 --scan-budget-seconds 45 --cache-dir .closure-work/nra-{before,after} --context-root src` followed by target paths. Before ran before edits; after on full D2–D4 source before disjoint main183 merge. Author-authored declaration/consumer changes, not a synthesized equivalence proof. Detector zero is not semantic completion evidence; current caller deletion and tests above establish the scope.

## Integration and preserved limits

Merge draft onto parent integration, retaining merged D1/current main. No paired UI contract or data migration required. Existing SQLite tables/index names and stored state strings, native receipts, source JSON and publication payload stay unchanged. Existing rows are read in place; no input/session replay or new witness synthesis. Old UNKNOWN remains blocking. Current admission weak receipts are deliberately process-local and cannot survive a restart/rebuild.

Parent owns serial actual deployment and any fresh configured-provider acceptance; no live roots touched here. Canonical-native explicit manual `/compact` guard remains unchanged. Issue107 remains issue-only; no cap implementation. No scheduler/queue policy, native four-call ceiling, models or selected provider route changed. Review harnesses must use contracts above rather than retained aliases.

Disposable artifacts will be cleaned after evidence commit/push, retaining receipts and executable tests. Published branch preserved.
