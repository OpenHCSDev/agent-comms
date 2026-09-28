# Post-PR95 ownership decision receipt

Darwin, 2026-09-28. Read dispatch `plans/POST-FEATURE-DEBT.md`, original S7 and current OWNER-DECISIONS. Source assessed: completed C0 declaration head6ed78c5 (includes lease174/checkpoint176); parent is integrating C0 operations178 and declarations179 independently. File sizes in the old census identify targets, not defects.

## Complete ownership trace

| Fact / effect | Actual current owner and consumer | Decision |
| --- | --- | --- |
| Trigger / configured model / effective settings | `owner_compaction_adaptive.maybe_compact_owner_turn`; normal caller `OwnedTurn.prepare_native`; selected Pi settings through `selected_pi_route` | Keep. Default native adaptive flow already exists. An injected test strategy is not the production selected path. |
| Native source cutpoint | `owner_compaction_prepare.prepare_native_source`; strict native loader and in-memory SessionManager; returns preparation metadata only | Keep read-only preparation separate from paid summary and native mutation. It cannot grant commit authority. Python witness representation has debt below. |
| Pre-summary source and final owner/ingress authority | `OwnerCompactionCommit._boundary/capture_source/prepare_source/commit`; Registration attestation, existing InputDispositions and FutureInputQueue | Keep. Capture-before-summary and recheck-at-commit concern changing facts, not duplicate static validation. Exact accepted future queue receipts are allowed; old UNKNOWN/corrections/owner change remain protected. No second queue/store needed. |
| Selected summary reservation / provider RPC | `SelectedSummarySlot`, existing S2 Pi channel and `CompactionJournal.reserve_selected_summary` | Keep selected identity/receipt owner. Provider output is not proof of native commit. |
| Runtime ordering / idle manager retirement | `compact_owner_once`, typed `OwnerSummaryOutcome`/`SelectedNativeSummary`/`SelectedSummaryDecline`; PersistentPiSession.discard_for_external_write | Keep independent from writer. Existing cancellation-resistant executor waits for the exact native commit; discard prevents stale in-memory state after file mutation. String-result adapter has debt below. |
| Native mutation authority | `OwnerCompactionCommit.commit/reconcile`, native writer CAS and exact commit receipt | Keep native writer authoritative. Python observation cannot replace independent native source/version verification. |
| Retained lock FDs and child deadline | `owner_compaction_process.run_authority_child`, gated launcher, pidfd watchdog | Keep. Watchdog survives parent death without authority FDs; child retains authority until exit. Ordinary manual process-group teardown is not an equivalent replacement. |
| Durable intent / selected reservation / metadata outbox | `CompactionJournal`, existing SQLite EXTRA transactions and post-COMMIT parent fsync | Keep one journal. It records exclusions and recovery, not bearer authority. Typed lifecycle duplication has debt below. A8 LockedStore JSON replacement cannot replace this multi-table transactional contract. |
| One-use original-input admission | `SelectedSummaryAdmission` consumes exact returned fsync ACK, process/source/identity checks; InputDrain binds original once | Keep. Weak identity receipts and persistent SQL rows represent different facts: a visible row after failed fsync must not mint a returned ACK. |
| Saved-session writer exclusion | `session_fence.session_writer_fence` / `idle_session_writer_fence` | Keep one existing lock namespace. Async normal lifetime and nonblocking inherited-FD lifetime differ deliberately; no new lock introduced. |
| Explicit manual operation | ACP/runtime -> `manual_compaction_bridge.compact_context` -> old manual procedure; old backend.compact_session had only one test caller | Original S7 omission confirmed and implemented below. |
| Native Pi proof-journal recovery cap | existing issue107 | Issue-only. Distinct from core CompactionJournal and private-bus checkpoint. No implementation here. |

## D1 — Original S7 manual transaction ownership: IMPLEMENTED by Darwin

Before: manual_compaction.compact_session -> _compact_session_under_fence carried profile, process, reader, source snapshots, stderr task, cancellation and result as procedure locals. Separate _response and compact loops repeated command-correlation policy. `backend.compact_session` was an independently callable second direct writer with weaker checks; repository source/stack/tools/scripts had no production caller. Its only test created an empty session and treated an RPC response as success.

Determining owner is now `ManualCompaction`: one explicit transaction owns request configuration, source bytes, private profile/environment, child/PiRpcChannel/stderr task, outcome and cleanup. It runs once, including refusal/uncertainty; another call on the same object cannot replay. Existing session fence surrounds the complete lifetime, including cancellation-safe reaping and durable session/parent sync. Typed GetState/Compact use existing PiRpcChannel/PendingRequests once per request; no custom second correlation registry.

Deletion and caller closure:

- Removed both old manual entry procedures, the duplicate _response loop and forwarding _summary helper.
- Removed backend.compact_session and its sole obsolete-interface test; no compatibility forwarding name.
- Migrated the one production manual bridge call, direct tests and interception hooks to `ManualCompaction(...).run()` / the owning method. Receiver-aware assertions inspect transaction inputs.
- Existing ACP/runtime `/compact` boundary, turn ownership/UI updates and saved result shape are retained. Bridge admission remains TurnRunner's responsibility; native writer state is not stored on that shared runner.
- Kept unique pure file/argument/result checks and the existing process teardown helpers. No phase service delegates to another mutable shared self.

**Acceptance:** focused argument/credentials/source refusal; same writer fence before child start; real local child GetState/Compact requests with unrelated-response filtering; output without durable row rejected; wrong command rejected; timeout/cancel reaps before fence release; exactly one compact send/no same-object replay; profile removed; ACP emits one terminal lifecycle and clears measured usage; native launcher and renamed-symlink manual refusal remains. Current evidence is in HANDOFF.md. No provider network, native bundle change or live data mutation.

**Feature boundary, not a completion claim:** normal canonical pi-native/pi-comms-native explicit `/compact` still rejects the older direct writer at manual_compaction_bridge. That guard prevents bypass of PR95 journal/native authority. Automatic PR95 compaction is already default-on and untouched. Supporting an explicit manual request through PR95 requires its own real admission design (manual has no original pending input to consume), not removing this guard. This refactor does not claim to deliver that feature or stock-Pi provider acceptance. The separate stock package pin still belongs to that existing direct-writer path; do not make it validate a different canonical native artifact.

## D2 — New PR95 journal lifecycle debt: REQUIRED NEXT PLAN, NOT IMPLEMENTED

Witnesses: `compaction_journal.py` Outcome/_TERMINAL, CompactionOperation.status, SelectedSummaryAttempt.status, CompactionPublication.status; `resolve` mixes terminal/unknown transition rules with row mechanics; SelectedSummaryAttempt.original_has_started has its own linked/declined test. owner_compaction_adaptive/commit and selected_summary_admission repeat committed/linked/declined case tests. SQL CHECK/partial-index predicates describe the same three lifecycle domains.

Fact owners to extend: existing journal records, A1 DeclaredFamily/A3 LifecycleState, not a new journal or global state-tags registry. Operations, selected-summary reservation and publication are **three distinct families**, not one catch-all state enum. Returned ACK identity remains separate from row state.

Complete scope: decode each SQLite status once into its actual family; put transition permission/terminality/eligible-original behavior on those declarations; derive SQL choice/predicate text where schema ownership requires it, preserving existing stored spellings and uniqueness; migrate get/unresolved/selected_summary/blocking/pending_publications, commit/adaptive, selected admission and all tests. Delete Outcome/_TERMINAL/string recovery branches and raw-status constructors. Keep actual saved rows, JSON evidence, native receipt validation, transaction/parent-fsync boundaries and UNKNOWN blocking. Do not manufacture new leases or replay orphaned attempts.

Acceptance: reopen existing rows for all states without rewriting history; exact native intent/unknown transitions; UNKNOWN->refused forbidden, immutable terminal outcomes; rollback/lost parent fsync never grants dispatch; linked row without returned ACK cannot mint original; pending/observed publication preserves metadata; original/future queue run once; old UNKNOWN remains blocked. Existing journal/commit/publication/selected-admission and local-native queue fixtures provide the cases. Proposed worker: Darwin or next existing worker after parent assigns this complete surface; not started. Implement before or serially with D3 because their consumer write sets overlap.

## D3 — New native witness boundary debt: REQUIRED NEXT PLAN, NOT IMPLEMENTED

Witnesses: `NativePreparation.witness: dict[str,str]`; prepare_native_source validates five exact keys; OwnerCompactionCommit._guard_arguments repeats that shape test, capture_source copies dict, source serializes it; runtime/provider/SelectedSummarySlot recover fields by string. These are repeated Python representation checks, distinct from necessary later native/identity revalidation.

Extend the existing NativePreparation owner with one A2-decoded immutable witness for session ID/file, leaf, kept-entry and native revision. External camelCase JSON remains the native wire contract. Migrate preparation, source capture/commit/reconcile, provider/runtime/selected-summary RPC and tests to that typed witness; delete duplicate Python key rosters/dict copies/untyped constructors. CompactionSource's persisted/canonical JSON remains a serialization boundary, not a second type registry. Native writer independently rechecks disk CAS; owner/ingress/current revision checks remain.

Acceptance: malformed/extra/missing external keys rejected once; typed current callers cannot construct a partial witness; exact old JSON shape retained; stale leaf/revision/session, inode change, source mutation, owner change and queued/UNKNOWN cases still refused appropriately. Existing prepare/commit/admission tests plus local native fixture, not new provider calls. Proposed worker: same next existing compaction worker, serial after D2 or one coherent combined change; not started.

## D4 — Detached summary/test-adapter closure: REQUIRED NEXT PLAN, NOT IMPLEMENTED

Witnesses: `owner_compaction_runtime.compact_owner_once` accepts str|OwnerSummaryOutcome and wraps str into NativeSummary; current normal adaptive callback returns SelectedNativeSummary/SelectedSummaryDecline. Raw strings are test strategy callers. `owner_compaction_provider.summarize_native` has no production src/stack/tools/scripts caller; only test_owner_compaction_adaptive imports/calls it. Its embedded _SUMMARIZE launches a second standalone provider transport, while actual normal runtime uses selected Pi RPC.

Extend/reuse OwnerSummaryOutcome and the selected summary owner. Migrate tests to typed outcomes; remove the str adapter/annotations and unused detached summarize_native/_SUMMARIZE provider transport and its transport-only tests. Keep NativeSummary, outcome behavior, valid_native_usage and NativeSummaryError where current selected/native consumers need them. Review the injected summary_strategy seam separately: synthetic outcome injection is useful test dependency injection, not permission to retain an unused production provider transport.

Acceptance: local synthetic strategy and selected-summary decline/commit still choose one outcome implementation; configured live path remains selected RPC, no alternative paid call; error cause/402 diagnostics, four-total-call native limit, no-replay and one-use admission untouched. Proposed worker: same planned phase closure as D3; parent schedules. No provider acceptance repeated here.

## Explicit exclusions / disposition

- No evidence that phase file count or OwnerCompactionCommit length alone requires splitting it into more services. The existing source/authority/summary/write/transport boundaries above are meaningful and remain.
- No generic process supervisor merger: pidfd/inherited-authority guarantees differ from ordinary RPC teardown. No generic JSON store substituted for the SQLite journal.
- Issue107 remains the existing bounded crash-atomic native proof-journal follow-up; no duplicate issue, raised cap, implementation or completion claim.
- C0 combined source and paired Toad deployment remain parent's active work. This branch edits only manual ownership/callers plus unused backend writer deletion. Backend's normal coding/session implementations are untouched.
- Parent folds D1 completion and D2–D4 pending decisions into dispatch POST-FEATURE-DEBT/index. This receipt is an audit plus one implemented original omission, not a claim that every post-feature debt item is finished.
