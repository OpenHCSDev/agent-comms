# Goal/coordination attempt ownership — Wegener

Assigned after parent accepted383; supersedes the old parent-owned goal-attempt row. Base main13d92f80. PR384 source review0e4e1475 READY posted separately; parent owns merge and paired live install.

Working whole checkpoint: 646 production lines deleted /620 added. Goal outcome mutation is now owned by existing Reservation and LaunchPermit; Generation and AttemptRecord own typed read/checked transitions; GoalHumanDecision owns unique consumption, Generation owns ready-grant digest interpretation, ProviderUsageTotal owns its projection. GoalAttemptStore retains the exact private connection, commit/fsync/fresh-readback and process-local grant/claim custody. No forwarded outcome methods remain.

AttemptStart owns atomic activation; actual AttemptState owns observed phase/finality and renewal; existing terminal state subclasses choose silent success versus retry-authorized/failed outcome. RecoverySnapshot applies coupled settlement to its actual execution/attempt/claims/obligation/pointer. ReplayAssessments owns monotonic proof recording. AttemptStore retains fence/transaction admission, no second store/schema/journal/lifecycle. Production recovery and DurableTurn callers migrate; success-bool settlement replaced by an actual terminal AttemptState, old APIs deleted.

Both schemas and current wire formats remain unchanged. No prior-format reader, reset, cutover/conversion or live mutation in this PR. A layout change would require current-only parent cutover; none is introduced to hide the ownership work. UNKNOWN never grants retry and original inputs are never replayed. Original declared SQL triggers and transaction orders remain.

Patterns: IDEN-1/3, IMPL-4/5/10/12, BOUND-1, MEMB-5, TIME-1/3. Existing declaration owners absorb their actual behavior; this is not new facade/handle/state duplication. Latest NRA/refactor-audit and round2 rules read. Prior whole-project plan census retained; resource warning excludes another broad NRA/native matrix.

First focused goal ledger+coordination checks79PASS9.10s. Broader affected crash/recovery tests running; installed saved-native ACP goal failure->explicit Retry->verified progress and actual selected UNKNOWN remain required before readiness. No CI hold. This draft is source/checkpoint, not live-ready.
