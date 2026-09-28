# Combined full-suite closure — in progress

Branch starts at2f445bd and includes parent9b01741. Parent's
SummaryState.require_commit_reservation restoration/guard is retained.

First serial full run: pytest -q -o addopts='' --maxfail=12,
55-second command bound. 12 failed,35 passed in10.67s. Exact failing node IDs
are in first-failures.log. All first failures are test_acp.py fixtures using
unconfigured canonical roots or retired text/public-drain assumptions.

Current code-bearing checkpoint:
- Canonical owner fixtures construct the current marker/root/package configuration.
- Existing goal/provider/session/relay behavior assertions are retained for execution
  on current owners; implementation failures remain visible.
- Seven tests removed: five arbitrary text-executable launch/cancellation/error
  cases, the disconnected public inbox steering case, and automatic adoption of
  a reopened unused goal grant. These paths were deleted by L0. Native preflight,
  exact native start/UNKNOWN, owner socket/steering and explicit Retry/no replay
  acceptance already lives in current native/input/goal tests.

No full-suite pass claimed yet. Continuing serial bounded failure batches and
publishing fixes. Parent owns reservation-method source and D22/installed gates;
243/244 own native producer/history closures, Cicero owns view_unread scaling.
No T2 edits. No live state, installed package or paid provider changes.

## Second failure batch

Full collection: 2,983 tests. Serial run stopped at 12 failed/83 passed in29.87s.
Seven failures were the owned basetemp exceeding the Unix socket path limit;
shortened it to .t in the same persistent worktree.

Real production defect: ScheduledTurn.take_batch and InputDrain.schedule_wake
read the deleted reply_target. Both now derive the route from origin through
existing wake.derive_exact_reply_target; no compatibility property restored.

Removed two obsolete public-drain echo tests. Current channel/input tests retain
real SQLite, native journal, owner socket, start/UNKNOWN and per-recipient receipt
assertions. Native forwarding uses the explicit local RPC executable fixture.
Goal metadata is decoded via Goal/FieldCodec; thread constructor uses ProcessIdentity.
Human DM notice expectation now matches canonical publisher's human-only policy.

Affected ACP/notification/input run:95 passed,2 failed in23.76s; remaining fixture
and metadata-filter assertions corrected and included in next full run.
No full-suite acceptance claimed.

## Third failure batch

Full run reached114 passed/12failed in43.74s. Actual canonical /compact router
refusal escaped as ValueError. Manual bridge now returns its existing failure
result for ValueError/CompactionJournalError, so ACP emits RequestError and the
finally path still aborts activity. Actual router test preserves saved bytes and
checks every launch form; no alternate unjournaled writer is admitted.

Activity/goal fixtures now use canonical_agent. Native forwarding asserts actual
thought/text/tool ordering and lifecycle identity, independent of added metadata
updates. Focused21-case run:18pass/3fail in13.22s. Remaining failures pinned retired
None refusal/internal goal-grant spending: current f81ac72 explicitly separates
fresh owner authority from autonomous goal grants. Assertions now require False
refusal and unchanged autonomous generation while retaining native STARTED/UNKNOWN,
owner-stop revocation, goal replacement and no-replay behavior. Next full run
includes those corrections; no production authority change.

## Parent ownership integration and persistence fixtures

Merged parent8aa4419 cleanly, retaining archival-read and real saved-collision
coverage. Parent alone owns publication collision boundary source and live stage.
Cicero owns checkpoint/source-cursor cleanup; no changes to those modules here.

Third full attempt reached cohort tests before its165-second command bound;
11 failures observed, no complete result claimed. Isolated failure batch plus
parent-reported fixtures:67 passed/1failed in14.37s after fixes. The final failure
called deleted MessageBus.remove_thread; that old-path assertion is now deleted,
as is deleted Publisher.publish acceptance. Canonical claim conflict, retained
history and marker behavior remain covered by the existing real publication tests.

- Registry family fixture gives BlockedGoal an explicit fixture-only reason.
- Visible-after-fsync fixture rewrites an actual canonical published row with0600
  permissions; it no longer inserts an unattested row outside the history boundary.
- Backend settlement exercises actual native RPC pipes via explicit fixture trust.
- Owner-pause tests use the canonical configured owner; assertions unchanged.
- Channel membership/pin fixtures use ProcessIdentity. Deleted unmarked legacy
  #any runtime-reader assertion; retained agent and human non-routability checks.

Next full serial run uses explicit -n0, cleared addopts, maxfail12, verbose log,
JUnit output, owned .t basetemp and1800-second overall bound with SIGINT cleanup.

## Fourth full run and publication closure

Full serial run:623 passed,2 failed,10 fixture errors in174.58s. Complete JUnit
was written under owned .artifacts/suite/full-fourth.xml. All earlier ACP,
backend, channel and actual manual native paths passed in that run.

Corrected command-family real-socket ProcessIdentity; reviewed CLI golden diff
adds only the already-declared compaction-status command/help. Publication fixture
now constructs CommittedNativeOutcome with its required fixture digest through
FieldCodec and canonical_agent. Actual delivery/cancellation/socket/rebinding
assertions unchanged. Focused command/publication batch:39 passed in21.64s.

## Remaining-suite continuation: declarations and removed public bus

Merged parent6422dcb before this continuation. Beyond compaction publication,
287 passed/12failed/1skipped in92.31s. Failures were canonical send-admission
setup, removed truncated-public-bus auto-repair, and registry PID fixtures.

Registry fixtures now carry captured ProcessIdentity. Preserved revocation,
rename, session metadata, restart-generation and saved-owner rejection assertions.
Removed old automatic bus-tail quarantine test; current envelope truncation
refusal tests preserve the fail-closed behavior.

Next actual declarations run exposed the deleted Publisher.publish-based class.
Deleted28 test functions (43 parameterized cases net), obsolete hand-made marker
helper, and private response-intent fixture; exact names in retired-public-bus-tests.txt.
These implemented the removed public writer/hybrid format, including automatic
metadata repair, raw-format keyed publication, and old bus deletion. Existing
canonical envelope tests cover fsync/UNKNOWN/conflicts/sequence durability;
coordinated runtime covers response append and stale/revoked owners; bus page
index tests cover bounded reads. One new canonical owner flow preserves current
sender/target validation, sequence uniqueness, cold reopen, alias DM paging,
self-exclusion, acknowledgement and later input delivery without full-log reads.

Affected run64pass/1fail/1skip in5.43s; the new test mistakenly expected latest
sequence rather than acknowledgement count (two pending messages), corrected
before the continuing remainder run. No source changes in this batch. Parent
owns candidate-wrapper and finite-JSON cleanup; those paths are not restored.

## Fresh native detach and goal fixture closure

Remainder segment365pass/12failed/4skip in60.91s. Goal fixture was reissuing an
already-created canonical marker; it now reuses the actual metadata identity
and prepared native package. CLI subprocess uses current imports; shared-wire
case asserts canonical DM history instead of removed public-drain transcript echo.
Focused CLI/goal-failure/standby batch71passed in31.14s.

Merged parent7ba2676; direct candidate scheduler and finite-wire deletion retained.
Found relative PYTHONPATH=src lets children launched in another cwd import the
parent editable checkout. Final acceptance commands now use absolute own src;
prior subprocess results with inherited relative paths are not final provenance.

Replaced old FIFO/text executor detach test with real CommsClient->detached
worker->prepared native Pi->loopback HTTP, no paid provider. It reproduced a real
startup failure: cohort_foreground._preflight rejected every root outside /var/tmp.
Removed that geography restriction; retained explicit activation, existing lexical
ancestry/UID/0700 session checks, package attestation and exact canonical root ID.
Actual test passes in5.30s: disconnect first client midrequest, attach two clients
to the same process, release exactly one loopback request, observe settlement,
leave owner alive until explicit cleanup. Owner launch result/process behavior
is not mocked; only stdout/stderr is redirected into the owned test log.

## Image and missing-goal-authority batch

Continuation129pass/3failed then interrupted deliberately at an unbounded fixture
wait after the prompt had already failed. Corrected canonical image owner setup
and bounded that wait. Native RPC executable fixtures now actually execute;
image stderr protection asserts child exit7, preventing a preflight refusal
from masquerading as a successful data-leak check. Current queueState restored
references and exact UNKNOWN pending IDs replace dropped-steer/old metadata
expectations; attachment images and original reference remain asserted.

Deleted implicit ledger manufacture on Retry expectation; actual runtime socket
now asserts rejection, unchanged blocked goal, absent grant and no scheduled wake.
Focused image/UI-goal batch16passed in2.10s. Continuing remainder after images.
