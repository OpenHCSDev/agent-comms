# Goal/coordination attempt ownership — Wegener

Assigned after parent accepted383; supersedes the old parent-owned goal-attempt row. Base main13d92f80. PR384 source review0e4e1475 READY posted separately; parent owns merge and paired live install.

Working whole checkpoint: 646 production lines deleted /620 added. Goal outcome mutation is now owned by existing Reservation and LaunchPermit; Generation and AttemptRecord own typed read/checked transitions; GoalHumanDecision owns unique consumption, Generation owns ready-grant digest interpretation, ProviderUsageTotal owns its projection. GoalAttemptStore retains the exact private connection, commit/fsync/fresh-readback and process-local grant/claim custody. No forwarded outcome methods remain.

AttemptStart owns atomic activation; actual AttemptState owns observed phase/finality and renewal; existing terminal state subclasses choose silent success versus retry-authorized/failed outcome. RecoverySnapshot applies coupled settlement to its actual execution/attempt/claims/obligation/pointer. ReplayAssessments owns monotonic proof recording. AttemptStore retains fence/transaction admission, no second store/schema/journal/lifecycle. Production recovery and DurableTurn callers migrate; success-bool settlement replaced by an actual terminal AttemptState, old APIs deleted.

Both schemas and current wire formats remain unchanged. No prior-format reader, reset, cutover/conversion or live mutation in this PR. A layout change would require current-only parent cutover; none is introduced to hide the ownership work. UNKNOWN never grants retry and original inputs are never replayed. Original declared SQL triggers and transaction orders remain.

Patterns: IDEN-1/3, IMPL-4/5/10/12, BOUND-1, MEMB-5, TIME-1/3. Existing declaration owners absorb their actual behavior; this is not new facade/handle/state duplication. Latest NRA/refactor-audit and round2 rules read. Prior whole-project plan census retained; resource warning excludes another broad NRA/native matrix.

First focused goal ledger+coordination checks79PASS9.10s. Broader affected crash/recovery tests running; installed saved-native ACP goal failure->explicit Retry->verified progress and actual selected UNKNOWN remain required before readiness. No CI hold. This draft is source/checkpoint, not live-ready.

## Ready checkpoint — current main383 included

Final production delta against13ff0385: **657 deleted /649 added**. GoalAttemptStore781→490lines; AttemptStore512→298. Stores retain actual durable connection and capability custody; domain transitions belong to existing record/state declarations. No class crosses500. Final ratchet has no positive delta: god-class excess−293, foreign absence probes−11, type-identity checks−1; chain terms and codec subclasses unchanged.

Production code tested at3ae275f3; later ready/cleanup changes are evidence only. Noneditable installed candidate `.artifacts/attempt-ownership/runtime`, matched immutable native9213 verified. Main381/382/383 integrated, native package untouched.

### Real-path evidence and corrected failures

- `focused-closure.log`:153PASS67.56s covering actual SQLite transactions, OS process-loss/claim/READY durability, late outcomes, native UNKNOWN settlement and goal failure observations. Earlier first scope79PASS9.10s.
- `installed-native.log` is honestly RED: first saved-native goal journey stopped before its third input because OrdinaryGoalGrantRule still called deleted `_is_attempt`; separate EOF case skipped because its native environment variable was missing. Fixed that actual caller via LaunchPermit.is_claimed, no alias or replay. Added mechanical guard for the missed helper and removed store facade methods.
- `installed-native-corrected.log`: **2 actual installed cases PASS22.20s**. Saved goal history→ACP load/new input→real native local HTTP503→passive failed projection→socket explicit Retry→exactly one new successful continuation. Four saved inputs, four local provider posts, original history and preexisting uncertain input unchanged. Selected saved history→real transport EOF→UNKNOWN retained→genuinely fresh input completed, no old input replay.
- A mistakenly broad final selector ran126 tests:125PASS,1SKIP,1FAIL31.29s. Only failure was the already-corrected383 fixture expecting Reserved after Done(false), when NotSent is correct. Integrated merged383 including its stronger distinct-new-input fixture; no weakened expectation or compatibility added. Receipt retained.
- `merged-installed-goal.log`: **11PASS14.94s** on current integrated noneditable wheel. Rechecks the actual saved-native goal/ACP/socket-Retry journey affected by383, deleted-method guards, uncertain usage durability termination, both preflight feedback states and invalid/live-phase settlement. No repeated selected UNKNOWN matrix after disjoint383 change.
- `installed-imports.json` proves site-packages origins and the corrected claimed-permit contract;9213 manifest verified. Installed wheel rebuilt/reinstalled without cache after383 integration.

No remaining diagnosed blocker in assigned closure. Parent retains source review, merge and paired physical live acceptance; this is not installed on the live owners. No new agents, paid provider calls, original input replay, reset, migration or CI gate. Existing proof/schema/public formats remain current and unchanged. Whole-plan completion is not claimed.
