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

## Native queue, maintenance and MCP closure

Remainder-four175pass/12failed/4skip52.59s. Corrected current GoalHumanDecision
SQL owner in Retry test and explicit local executable trust for maintenance/MCP
protocol fixtures. Installed lockfile MCP test deps offline in this owned tree
(npm ci --ignore-scripts --offline;187MB, remove after final suite).
MCP13pass8.96s including package SDK/stdio MCP/ACP permission and receipt paths.

Both actual native summary->original->queued followup cases pass (16.10s combined
with then-failing maintenance test). Their obsolete subprocess interception tested
command=pi, but current attested launcher produces node. Interception now applies
at current NativePiRpcLaunch.managed boundary, preserves attestation/session
selection, and uses prepared native SDK/RPC whose model host prohibits network.
Assertions retain exact native IDs, one start each, saved compaction order and
foreign UNKNOWN. No production queue behavior weakened.

Deleted fake PID/process/raw writer in maintenance native test. Real local child
writes pause ACK before capability response; actual fenced prompt writer refuses,
child is reaped and persisted runtime admission remains.1pass0.74s. This reproduced
SelectedExecution._open's stale /var/tmp-only restriction; removed geography only,
retaining explicit activation/private directory/package/root identity checks.
Coordinated runtime module62pass25.48s (same run initially exposed test socket
path length; final real maintenance fixture uses short owned session directory).
No publisher/checkpoint/current-source changes. Full suite still not complete.

## Parent251 integration and actual raw-send closure

Merged58bfad3 into248 asd66588e; parent bootstrap/cursor deletion retained. Parent
continues to own cutover tools. Remainder-five164pass/9failed/3skip before deliberate
interrupt150.25s after the first repeated90s raw-send timeout. Current native
binding tests pass after parent251 retired the obsolete cursor/alias expectation.

Raw-send fixtures intercepted asyncio.create_subprocess_exec and bypassed the
current POSIX exec gate, leaving its inherited startup error pipe open. Fixtures
now call real AttachedChild.start with their local protocol executable, preserving
identity/gate/cleanup and actual raw fd writes. No fence or proof assertions removed.
Includes bounded partial-write/cancel/repeated-cancel/lifecycle and contention
subprocess cases. Added explicit executable trust to proof-journal tests; no native
size-limit behavior changed. Mentions derives reply destination from existing origin.

Focused combined result65pass/1skip24.46s: mentions, current native binding,
proof journal and all23 real native send admission cases. Full suite remains pending.

## Startup, operations, adaptive compaction and real native writer batch

Remainder-six209pass12failed209.83s. Large native cursor1001/>8MiB and N150 cases
passed on parent251. Corrected local RPC trust for startup cancellation tests;
current ProcessIdentity fixtures for managed rename/stop and current thread detail;
disabled writer preserves canonical initial header bytes instead of requiring no
file. Adaptive ACP fixture now uses configured canonical root and prepared package.
Malformed compaction ingress test first creates a canonical root, then proves
corrupt bytes remain unchanged. Competing bus writer uses canonical Comms send,
retaining actual cross-process bus lock contention through native commit.
Deleted its retired Publisher.publish invocation and raw Message imports.

Focused109 tests:108passed1failed159.13s; all37 actual owner native commit cases
pass. Last failure expected fresh owner correction to block autonomous goal.
Updated to preserve exact goal and grant, retaining UNKNOWN input/no native send
and no compaction. Both corrected adaptive ACP cases pass8.97s. No production
changes this batch. Complete full suite and remainder after owner commit pending.

## Current native243/244 and main252 integration

Merged parent1aef899 as8d02449; resolved modify/delete by deleting
 tests/test_native_proof_journal_limit.py. Its former fixture fixes are superseded
by244's deletion, never restored. Merged parent9e3dc3c main252 pins afterward.
Current acceptance package is
/home/ts/wt/comms-native-session-entry-store-20260928/stack/.pi-native-0d7ebb4f4b5aa1ec/node_modules/@earendil-works/pi-coding-agent
read-only. Prior prepared b3a9c06 receipts are historical, not final-current proof.

Fixed compaction nonowner fixture to use real other process identity. Current
managed Pi reopen validates corrupt session before any provider CLI spawn;
cancellation/repeated cancellation/all-tasks tests retain actual native commit
quiescence assertions. Current native source helper's error contract replaces old
window text. ACP three-round publication and actual owner start/restart tests now
use explicitly configured canonical root/package. No model prompt in lifecycle test.
Focused current-package result34passed2existing-skips70.67s across owner gate,
compaction preparation and actual owner lifecycle. No production changes.

Parent-transferred test_view_unread: captured-basis race now enters through actual
ReadLedger.mark_displayed; registry joins during write still leave unseen DM unread.
Deleted old v1 marker migration test.6passed2.41s on main252-combined tree.

Concrete remaining: test_passive_channel_awareness still expects old advisory
ledger initialization. Production has no initialize caller, but OwnedTurn and
channel_management retain consumers.11 failures observed in affected partial
run; parent global L0 owner notified with exact source references for ownership.
Do not silently recreate old cursor engine. Other remainder after passive tests
and final complete suite still pending; no full-suite pass claimed.

## Project, queue, ingress reservation and routing closure

Remainder-eight301pass12failed119.72s on current native package. Human ingress
failure injection now targets write-only append after reservation, permitting
canonical pre-publication read verification. Exact UNKNOWN reservation/no replay
and later-gap behavior pass. Replaced bus inode is refused by canonical checkpoint;
read-ledger test preserves no inherited read facts instead of accepting replaced bus.

Project/queue/reply fixtures use explicit canonical agent; queued waits bounded.
Invalid queued Pi command fixture grants only local executable trust. Focused52:
50pass2fail11.46s; both routing defects then corrected, module5pass2.42s. Routing
checks use current OwnedTurn origins/reply targets rather than removed public drain;
retain live route, entry-ID persistence and equal private text isolation. Removed
monkeypatch of deleted acp.backend import. Actual channel delivery remains covered
by canonical ACP/native suites. Passive fixture changes remain uncommitted pending
parent source disposition. Independent remainder after reply routing continues.

## Runtime attachment and restart fixture closure

Remainder-nine15pass12failed1skip14.77s. Current response-policy test drops its
retired disposition_key/bus_key mechanism, retains nominal eligibility/guidance/
batching. Actual owner restart uses pinned canonical root/package, preserving
session and collaboration bytes. Runtime client fixtures configure current route;
socket-only owner identities use explicit ProcessIdentity, not deleted Thread.pid.
Removed115-line text-executor fork/drain test; actual native detached-owner test
already proves turn survival and concurrent client attachment. Long-root socket
roundtrip uses current owned input callbacks with real RPC server/client, keeping
prompt/settlement/cancel/compact behavior; actual bind replaces arbitrary<100 test
since owned persistent TMPDIR yields107 bytes accepted by this OS.

Focused13:12passed1failed10.61s; corrected long-root test1passed0.83s. No production
changes. Passive module explicitly relinquished to Cicero, own setup-only diff
saved at.artifacts/passive-fixture-config.patch then reverted. Direct CLI delivery
to Cicero rejected (no visible executable); parent notified to relay. Current
independent remainder continues after runtime. Final combined suite still required.
