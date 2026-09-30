# Native pre-admission contention closure

Owner: Einstein, continuing the merged431 native lifecycle closure. Scope: the actual215 native05 selected reply followed by channel triage failure at native_prompt_send._enter_admission, before any prompt bytes. Preserve the original diagnostics, journals and uncertain inputs; never retry them.

Trace the existing raw writer, canonical exclusion lock order, coordination connection/transaction lifetimes and all native send callers. Fix the proven ownership defect in place, not a cap increase or another timeout/watchdog/state store. Keep Arendt's admission identity domain and Mendel's outbound reply-source closure separate.

Acceptance: the existing installed selected/native channel user journey with controlled localhost provider, first reply followed by new channel delivery, canonical fence/input proof and child retirement. Native fixtures run serially. Source checks supplement the real journey; CI is deferred. This initial draft does not claim diagnosis or readiness.

Persistent worktree: /home/ts/wt/comms-native-admission-contention-20260929. Base: ea7cbaed, currentmain with merged433 admission domain. Exact preserved diagnostic locations requested from215 owner before further evidence claims.

## Actual cause and working checkpoint

The noneditable installed three-window ACP/native journey reproduced pre-byte uncertainty. Preserved evidence is `/home/ts/.cache/agent-scratch/comms-native-admission-contention-20260929/probe2/canonical-wire/diagnostics`: alpha `ddfe65308cc6b9de8fda70e514ee64a4.json` failed at the SQLite schema-open COMMIT; beta `9c6189b2b0c4cf667d465ed43ab18758.json` failed at the nonblocking wire flock. The journey failed its duplicate-reply assertion and is not acceptance. Fixture processes were retired; no uncertain input was retried.

`CoordinationStore` now validates an existing immutable schema in one consistent read transaction. Only fresh schema installation takes a write transaction, rechecking after acquisition. `SchemaMeta` owns the expected record and validation. No schema format, cache, timeout, alternate reader or admission state was introduced. This removes the unconditional writer COMMIT before canonical exclusion. Four focused real-SQLite checks pass in 0.42 seconds: concurrent reader/reserved writer reopening, version/privacy/reopen, malformed version/symlink rejection and first-create descriptor lifetime. Production checkpoint deletes 32 lines in the database opener and moves the metadata comparison to its declared owner; final counts are measured separately from imported430 work.

Parent's original 41 MB history recording/profile lives at `/home/ts/.cache/agent-scratch/comms-live-repro-20260929-original-read`. It shows repeated source certification/notification reads and lock pressure during scrolling and idle, plus a blank final chat. Mendel owns430's canonical bounded read transaction, indexing and publication closure. Candidate acceptance must include that original history; a small controlled seed cannot establish this large-history behavior.
