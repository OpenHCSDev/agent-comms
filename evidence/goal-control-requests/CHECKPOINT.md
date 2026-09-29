# Interrupted goal-control ownership checkpoint

Priority superseded by urgent intermittent NativeGetState cold-fork diagnosis.
Existing request declarations now own set/edit/update/retry; corresponding four
TurnRunner methods removed. Changes are unfinished and NOT ready for merge.
Source/socket run: 15 passed, 1 failed (socket-first.log).
Remaining test `test_busy_retry_keeps_unresolved_attempt_and_owner_fences[owner]`
injects owner revocation by monkeypatching the deleted sessions.require call.
Migrate that race injection to the canonical mutation boundary; do not weaken
its actual owner-revocation refusal assertion. Complete real native/installed
acceptance and review before readiness. No production deployment performed.
