# S2/T4 reusable native lifecycle: ready for parent review

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
