# Final current-main/#323 receipt: ready

**Integrated checkpoint:** `45d39ac5a587b14ab4e5c31ce38d9ec5d9432049` includes
main/#323 `658b9a89`, #328, and partial-snapshot correction `23e6c542`.
`review325-main323-native.log`: **5 passed in 69.69s**, actual pinned native/local
HTTP, no skips or paid calls:

1. Selected observation cancellation, exact child retirement, strict reopen,
   foreign-package/stale revision refusal and no replay.
2. Cancelled retirement joins the existing cleanup task before the next borrow.
3. Ordinary ACP selected summary commits and admits the exact original once.
4. Private ACP selected summary commits and admits the exact original once.
5. Attached ACP manual compaction: real owner socket, four provider requests,
   one commit, same-child reuse then retirement/reopen, updates and cleanup.

Only this critical matrix was rerun for #323's owned-turn/input/progress changes.
No unchanged full matrix or CI wait. Parent owns noneditable staging/live install.

## Exact channel fixture diagnosis and owner

`review325-stack.log`'s missing `bus:2:owner:...` row is **stale fixture semantics**,
not a #325 custody regression or a new #318 batch behavior. The routing already
existed before either PR:

- `git blame` at pre-#325 main `da2b43d4` assigns `InputDrain.drain_owned_inbox`
  lines 348–351 to `36a7c393` ("Certify fresh bootstrap and delete alternate native
  source readers"), an ancestor of #318. It calls `_drain_private_if_changed`,
  then `CommsAgent._drain_private_nk`.
- `_drain_private_nk` selects a sealed wake from `Coordination`. The coordinated
  full-send reservation is `_reserve_full_input` in `coordinated_runtime.py`:
  it inserts `NativeRuntimeInput` in coordination SQLite and binds the expected
  prompt. It does not insert an InputDispositions bus reservation.
- The old test assumes `drain_inbox` merely stages an ordinary pending turn,
  asserts the old bus-disposition row, then manually calls `schedule_wake`.
  Canonical private draining performs the selected native delivery itself.
  Disabling `schedule_wake` does not disable that route. The later expectation
  of reusing the ordinary warmup child is also not proof for a private wake.
- #325 changes none of `input_drain.py`, `acp.py`, `coordinated_runtime.py` or
  `native_pi.py`. The diagnostic canonical fixture migration reached that route
  and failed precisely at the obsolete row assertion; it was not retained.

**Fix owner: Dalton**, existing original-plan baseline closure; notified on #328
and #325. Migrate the old channel/goal acceptance to canonical wake/native input
evidence, preserving delivery/revocation/no-replay assertions. Do not manufacture
ordinary bus rows or restore the retired drain path to satisfy the old test.
This does not claim every later assertion in those eleven old nodes is valid.

---

# Review closure on #329 and #328

**Production/test checkpoint:** `23e6c54289ab0a85ab621f35929e17e63e1b6b22`.
Merged Dalton #328 (`1c7f53a7`) and current main/#329 (`da2b43d4`); PR325 targets
main. Parent owns merge/install after the live testing quiet window. No CI wait.

Reread both skills and the corrected **22:16** catalog, including IDEN-1/3,
TIME-9 and IMPL-14's dominant-kind rule. Dalton's partial-identity finding was
valid and is corrected:

- **IDEN-1 / IDEN-3 / IMPL-5:** StateData and SessionStatsData inherit the one
  external `NativeSessionSnapshot` declaration. It owns their shared optional
  identity fields, known-component conflict behavior and complete identity
  projection. Components derive from `fields(NativeSessionIdentity)`, with no
  second roster. Attestation delegates to the snapshot instead of inspecting
  its individual nullable fields. A missing/empty component says nothing;
  complementary partial responses never synthesize a complete identity.
- **TIME-8 / TIME-9 / BOUND-1:** external snapshots keep their actual partial wire
  semantics. No codec subclass, internal parallel record, schema alias or
  compatibility reader was introduced. The existing complete identity remains
  non-nullable and validates its saved-session path.
- Caller closure includes indexed `persistent_backends[...]` in the three stack
  tests, in addition to all direct persistent/session consumers. Actual child
  assertions use retained custody; post-compaction absence uses availability.
  No remaining source/test access to the removed persistent child fields was
  found by the explicit direct/indexed caller search.

## Final affected evidence

- `review325-observation.log`: **31 passed, 36.43s**. Twenty real-codec partial
  snapshot cases, ten actual pinned-native observation cases, and one actual
  native identity-attestation refusal. The native cases use local HTTP only.
  Nine observation fault cases damage or withhold a response only after the real
  native process has emitted it: wrong correlation/session/model, invalid window,
  reserve/trigger/keep values, extra authority field, and deadline. Each retires
  the exact child, preserves history, starts no original input and never retries.
  Dalton's healthy/cancel/reopen/stale proof remains, with package refusal added.
- `review325-attached.log`: **1 passed, 23.66s** on the committed final code.
  Actual owner socket and attached ACP client: two inputs reuse the same child,
  selected summary supplies one native commit, old child is reaped, a new explicit
  input reopens compacted history. Exactly four local provider requests, one
  compaction, required attached updates, durable commit, socket/child cleanup.
- Ruff and diff check pass. Current-main R0/T4: **zero existing-measure increases**;
  chain terms **-81**, no touched-file increase. StateData and SessionStatsData
  each shrink two lines. Full current-main change: **445 production lines deleted,
  515 added, executable lines +7**. See `review325-measurements.json`; earlier
  checkpoint counts below are historical, not the final totals.

The first receipt preserves my missing-newline test error and eleven baseline
stack fixture failures; it is not green. A diagnostic fixture migration then
passed attached compaction and reached channel warmup, exposing its obsolete
pre-N/K bus-disposition assertion. That diagnostic migration was not retained.
Only custody caller updates remain in channel/goal tests. **Dalton owns their
canonical delivery/goal fixture and assertion migration** under the existing
baseline assignment; concrete failures were posted on #325 and #328. There is
no claim these eleven nodes pass and no compatibility restoration to make them
pass. No remaining observed production failure in this review scope.

## Deleted-test behavior mapping

The removed `tests/test_selected_pi_route.py` exercised an unused Python dry-run
API and also useful read-only observation contracts. Those are distinguished:

| Deleted test | Current coverage / reason |
| --- | --- |
| `test_ready_is_non_authorizing_selected_existing_child_only` | #328 `test_actual_selected_observation_retirement_without_input_replay`: actual settings read on the same child, unchanged saved bytes/provider count. The removed `SelectedPiDryRun` boolean prohibition and unused preparation request shape have no remaining Python caller. |
| `test_declined_is_bounded_not_a_paid_summary` | Actual `test_acp_selected_summary_handoff_uses_final_prompt_once[decline-*]` in the prior 33-pass receipt and durable decline/admission tests. The old dry-run `split_turn` refusal is obsolete: current combined-summary support includes the prefix. It is not restored. |
| `test_malformed_or_overclaimed_ready_poison_old_child` | New actual `test_actual_selected_observation_untrusted_receipt_retires_without_replay` correlation/window/extra cases and `test_selected_observations_keep_strict_envelope_and_payload` at the codec boundary. Deleted dry-run route-status spelling has no current consumer. |
| `test_timeout_is_unknown_without_replay_and_requires_reopen` | New actual observation timeout: native emits its response; held delivery expires; exact child reaped, unchanged history, retained strict-reopen requirement, no retry/input. |
| `test_cancelled_sent_probe_poisoned_without_retry` | #328 actual observation cancellation, then a separately explicit new input through validated reopen; no original replay. |
| `test_stale_child_refuses_before_rpc[revision/package]` | #328 actual retained revision refusal and added foreign-package refusal, with native history/provider/input counts preserved. |
| `test_selected_settings_reads_once_without_starting_or_writing` | #328 healthy actual-native settings observation; existing actual custom model/project settings test in the prior 33-pass receipt. |
| `test_selected_settings_uncertain_response_retires_child` | New actual-native session/model/window/reserve/trigger/keep/extra receipt fault cases. `contextTokens` was deleted from the protocol; strict unexpected-field rejection replaces any historical request/response use. |

The deleted no-child `test_owner_summary_discards_idle_manager_before_external_native_write`
is replaced by the **actual attached ACP compaction** case above and existing
actual canonical manual compaction proof: the retained child is alive while
selected summary is pending, retired by commit, and a new child reads the
committed branch. No fabricated identity on an empty PersistentPiSession remains.

---

# S2/T4 reusable native lifecycle: earlier ready checkpoint

**Code:** `a567b9fc3decf7bcef15eff77442221850b4a3cb`, draft #325, based on integrated
#322 `fe76c50e`. Parent owns merge and live installation. No deployment/restart here.

**Latest skill actually read:** both Codex skill paths; refactor-audit resolves to
`~/.local/share/agent-comms/skills/refactor-audit-20260928-2216/refactor-audit`.
Read updated SKILL, pattern README, IMPL-14's classification section and full
`chain_terms.py`; relevant full identity/implementation/over-time/boundary patterns.
The 18:41 source is superseded. No CI wait.

## Ownership and deletions

**441 production lines deleted, 496 added; 485 test lines deleted, 212 added.**
The extra 55 physical source lines declare legal custody/admission/attestation
states and their evidence; executable code lines fall by two. No parallel path.

- **IDEN-3 / IMPL-10:** `PersistentPiSession` owns locking/borrowing; custody states
  own the actual `PiSessionChild`, retained native identity/revision or shielded
  retirement task and successor. Empty/retiring/reopen states have no process,
  reader or saved revision fields. Cancellation joins the same retirement task;
  it cannot waive required reopen. Delete `reusable`, `_close_task`, nullable
  proc/reader/stderr/key/session/revision fields and separately mutable reopen flags.
- **IMPL-10 / IMPL-4 / IMPL-5:** native prompt response, original start and settlement
  advance `PromptAdmission` states. Attestation has pending/observed/lost states.
  Delete `can_retain`, initial-input/ack/session-observed/identity-uncertain/settled
  flags and initial-session field copies on `TurnSession`. Retained status comes
  directly from custody, not another boolean. `PreparedSession` admits retention
  after actual idle attestation without inventing a user start.
- **IDEN-1:** a reused/strictly reopened expected `NativeSessionIdentity` is compared
  as one value. Optional fields in external Pi metadata still have their actual
  partial-response semantics; they are not fabricated full identity evidence.
- **IDEN-3 / IMPL-5:** selected settings and selected summary RPC both borrow the
  actual `RetainedNative` proof. Delete their duplicate 11/12-term nullable child
  checks. Stale proof refuses before reserve/write; uncertain transmission retires
  the child and retains the existing UNKNOWN/no-admission contract.
- **TIME-6:** delete unused `probe_idle_selected_pi`, `_read_response`,
  `SelectedPiDryRun`, `_request`, and their fake nullable-host test suite.
  Summary uses the already decoded `SelectedSummarySource.selected/settings`
  directly, deleting encode/read/decode through a throwaway preparation command.
  Native external command declarations remain the external protocol vocabulary.
- **IMPL-5:** `InputForwarding` owns its actual queue and derives unresolved input
  from pending items and that queue. Delete no-op drain/requeue/clear restoration,
  `restore`, `_ACTIVE_INPUT_RESTORERS` and its registration/termination callers.
  Already-written input is never put back on the queue.
- **TIME-9 / BOUND-1 / AGENT-8:** no new codecs, adapters, aliases or alternate
  stored formats. Existing Pi decoding, AttachedChild supervision and PiRpcChannel
  framing stay authoritative. Used packaged census/overlay/ratchet/NRA tooling.
  **IMPL-14 was not misapplied:** no rule subclass for each lifecycle bit.

Caller closure: backend/Pi command+event consumers, watchdog/output/failure/stats,
NativeSessionPreparation, both selected RPC callers. Only two production lines
cross #319: manual/adaptive `persistent.proc is None` becomes
`not persistent.available`. Test fixtures enroll an actual host as retained
custody, sharing its one reader/stderr task. No owned_turn/turn_runner production
or Toad edits; Boyle acknowledged disjoint ownership on #323. One turn-runner test
assertion follows the changed custody owner. Historical data and runtime schemas
are unchanged; parent may install with its normal quiet owner restart.

## Verification, at its actual strength

- `final-native.log`: **20 passed, 49.12s** on `b5d22c68`: nine actual pinned-native
  cases (queued reuse, >2 MiB response, cancellation, EOF, identity refusal,
  credential/session revision retirement, strict reopen, cancelled retirement,
  cold preparation), plus typed/new-command/recorded-input tests. No provider credits.
- `private-second.log`: **33 passed, 246.04s**, checkpoint before final custody
  projection/dead-wrapper cleanup. Actual selected native hosts with local HTTP:
  ordinary/private original admission, correction, queued/revoked future input,
  decline, custom settings, disconnect, manual commit, 400/429 failure settlement,
  existing private retained attachment after runtime-journal reset. No replay.
  This was a long aggregate suite; individual cases used existing bounded waits.
- `selected-final.log`: **5 passed, 22.38s** on final `a567b9fc`: actual manual
  commit, original admission, known 400/429 failures and uncertain disconnect.
- `owned-fifth.log`: **44 passed, 2 skipped, 37.11s** at the earlier checkpoint:
  actual native image privacy, failure/retry/compaction/watchdog paths, plus
  recorded RPC contract and declaration-extension tests. Two optional older native
  fixture cases lacked their separate environment opt-in; they are not passes.
- `queue-final.log`: **15 passed, 32.04s**, actual native plus recorded settlement
  and strict native reopen tests after deleting queue restoration.
- Recorded backend caller closure: `backend-callers-two.log` has **151 passes**
  before one stale mock target stopped it. `backend-remainder.log` then has
  **32 passes**, and the final-native shard covers the remaining native-input
  cases. Mock target followed the moved strict-reopen owner; no assertion weakened.
  These recorded cases do not substitute for the actual native cases above.
- `lint.log`, diff check: pass. **R0/T4 zero increases**, including independent
  existing class sizes. PersistentPiSession -106 lines, TurnSession -34,
  NativeAttestation -18, InputForwarding -1. New states measured as new declarations.
- Corrected 22:16 census: **chain terms -81**, no touched-file increase; long
  conditions -8, string subscripts -2, None tests -28, isinstance calls -7.
  Full per-file numbers in `measurements.json`; raw audits remain in owned artifacts.
- NRA completed requested full source context (241 files) in 40.469s, 110 raw
  findings, before final dead-wrapper deletion. No raw finding names new custody
  or admission owners. Existing `_tool_title` raw shape remains outside this scope.
  CLI omits analyzed/omitted detector counts and scan_status; no zero-omission claim.
  Semantic patches were authored, not claimed as mechanically proven DSL rewrites.

Failed/intermediate receipts are preserved. Initial missing artifact directory,
old custody test attribute, old mock target and duplicated fixture stderr consumer
were corrected. The final selected run has no duplicate-reader warning.

## New-case / remaining boundary

A new terminal failure still affects retention through the existing failure family
(actual native extension case passed); no retention roster or extra dispatch edit.
Custody retirement/reopen and admission transitions live on their state owners.
No known remaining functional failure in this assigned scope. Live installed
acceptance and merging are parent-owned; current running owners were untouched.

---

## Initial dispatch receipt (historical)

# S2/T4: native reusable lifecycle

Base: integrated #322 `fe76c50e`. Owner: native backend sidecar. Parent owns deployment.

Reread the corrected 22:16 refactor-audit install, both skills, pattern README,
IMPL-14 and `scripts/audit/chain_terms.py`. Classification: persistent nullable
custody is IDEN-3; retention's scattered flags are IMPL-10; comparing native
identity parts separately is IDEN-1. These are runtime state, not stored formats.
Pi RPC and native saved history remain external contracts.

## Implementation scope

Replace PersistentPiSession's nullable child/reader/error-task/session/revision
bundle with lifecycle states that own the actual child and retained proof.
Retirement owns its shielded task and required reopen; cancellation cannot lose
its process handle or waive strict reopen. Private RPC borrows the same retained
state instead of repeating nullable-field validation. Turn retention follows
observed native input lifecycle and existing output/input/stats facts.

Delete `reusable`, `can_retain`, public nullable custody fields, duplicate private
idle checks, copied turn admission flags and field-by-field identity checks.
No aliases, default compatibility paths, per-boolean rules, or parallel registry.

Crossings: Boyle owns #323 owned_turn/turn_runner/progress; Carver owns Toad.
Neither surface is edited here. Two existing compaction preparation call sites
must change their old `persistent.proc is None` query to owned availability;
these surgical changes will be explicitly identified for #319 integration.

## Acceptance

Actual pinned native reuse, auth/session-change retirement, cancelled retirement,
strict reopen, cold compaction preparation and selected summary/commit. Preserve
no replay, exact input binding and uncertain-outcome refusal. Local loopback only.
Touched-file chain terms and independent class-size ratchets may not increase.
No live restart or installation by this sidecar.

Status: implementation in progress; no new verification claimed.
