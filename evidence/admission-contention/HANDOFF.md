# Native admission contention repair

Parent-owned worktree: ~/wt/comms-native-admission-contention-20260927.
S2 worker retains backend/RPC decoding and native_pi.py; historical-view worker
retains read/history consumers. Neither worker's files were edited here.

Live observations before this fix:
- UX source18 completed TRIAGE/FULL in7.25s; PR95 subscriber remained deferred
  with no recorded send epoch or context.
- Source20 repeated the PR95 failure and published a generic native failure
  notice; configured native get_state preflight succeeds for its worktree.
- Existing diagnostics do not retain the wrapped cause. Local reproduction
  established both matching failure paths: fleeting wire/bus/registry flock
  contention yields NativePiUnavailable, and SQLite contention escapes as
  OperationalError before a native prompt. All four 100ms contention cases
  failed on the previous implementation with zero received prompt bytes.

The existing dedicated raw writer now owns bounded pre-admission waiting using
its existing deadline and cancellation event. PromptAdmissionBusy is raised
only while acquiring canonical exclusion, before identity checks, journal
reservation, admission binding or raw writes. Every failed probe releases partial
locks. Once all locks are held, the complete current ownership checks run and
one-use admission is consumed. No started/post-write attempt can reenter.
No new scheduler, durable retry queue, native process or model call is created.
Existing uncertain source18/source20 rows are not retried or rewritten.

A broader raw-send test found an existing error masking bug: if an owner was
revoked during a failing send, failure-notice publication replaced the original
native error with StaleFence. Baseline main reproduces both lifecycle failures.
Now an obsolete owner skips publication and the original failure propagates.

Validation includes actual pipe bytes under contention on all four stores,
revocation while waiting, cancellation/deadline before any bytes, post-write
failure with one admission only, partial-write cleanup, inherited owner locks,
coordinator state and ordinary N/K delivery. These local child fixtures do not
claim provider acceptance. 89 focused tests passed in27.75s; Ruff and diff checks passed.
Installed real-owner delivery remains to verify.
