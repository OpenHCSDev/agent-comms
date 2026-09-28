# S13: child supervision (#232)

Branch `refactor/round2-s13-child-process`, worktree
`/home/ts/wt/comms-refactor2-s13-20260928`. Audited against main #225 and rebased
through #231. Typed registry dependency #235 is merged into this branch for
real owner tests. Do not attribute its loader/table changes to S13.

## Ownership

Parent explicitly assigned threads.py ProcessIdentity to S13; Copernicus owns
thread_management/registry_document. S13 migrated backend, turn_inputs,
owner_lifecycle, recovery_gateway, Thread, foreground entrypoints and CLI
registration. input_drain now owns its existing polling interval after removal
of its import from the retired process supervisor. No native_pi,
selected_pi_child_deadline or compaction edits; S10/S9 own their adoption.

## Stable A12 API

- `ProcessIdentity(pid, start_time)`: A2 dataclass; `capture(pid)` only at the
  OS input boundary; `alive()` compares the saved birth time.
- `Thread.process_identity: ProcessIdentity|None` replaces stored pid;
  `.pid` is an OS-number projection, `.process_alive` uses full identity.
  Current JSON is derived through A2, with no old-format reader.
- `AttachedChild.start(tuple_argv, cwd=None, env=None, pass_fds=(),
  input_enabled=True, limit=65536)`: async stdin/stdout/stderr plus identity,
  pid, returncode. `await wait()/stop()/finish()` return ChildOutcome.
  stop retires the whole group; finish allows EOF flush before the shared grace.
- `BoundedRun.run(tuple_argv, timeout=seconds, input=bytes|None, cwd/env/pass_fds)`
  returns ChildResult(outcome, stdout, stderr). `BoundedRun.session(tuple_argv,
  timeout=seconds, **AttachedChild.start_options)` owns a bounded protocol
  exchange; callers retain actual external Pi/ACP parsing.
- `DetachedProcess.launch(tuple_argv, cwd/env/output, before_start=callback)`;
  callback commits ProcessIdentity before any executable runs.
  `attach(ProcessIdentity)`, `stop_sync(guard=contextmanager_factory)`,
  `await stop()`, `force()`, `alive()`. No bare-PID attach or stop.
- `NamespacedChild.start(tuple_argv, deadline=absolute_monotonic, cwd/env)`:
  typed PID1 readiness, release only after independent pidfd watchdog is armed,
  verified namespace retirement including a setsid descendant. Explicit Linux
  capability, refused elsewhere. ChildCommand declarations own child commands.
- `ParentLifeline`: context owns inherited read/write descriptors; environment
  and read_fd passed to child; child calls guard() before blocking work. EOF
  retires the child even while SQLite blocks its main thread.
- ChildOutcome family: Exited, Signaled, TimedOut(termination), FailedToStart,
  GracefulStop(termination), ForcedStop, DetachedExit (all suffix Outcome).

Platform family owns Linux pidfds/namespaces, Darwin libproc birth records,
Windows suspended launch + named job assignment before resume. One shared
2-second TERM/force/reap plan, sync and async. Failed reservations reap the
unreleased child. Windows graceful console control is best effort; forced
retirement requires the exact named job, with no bare-PID fallback.

## Cross-surface closure

- #234 / S10 has migrated pi_events and native_pi to A12 and deleted the test-only
  selected guardian. Integrate its branch with232; no deleted-helper calls remain
  there. Its reproduced repeated-cancellation gap is fixed below.
- #236 / S9: adopt NamespacedChild/watchdog and delete lifted local machinery.
- #235 / Copernicus: Thread constructors/replaces use process_identity;
  RegistrationChange identity decisions compare full identity, not only PID.
- #237 / S12: release reconciliation reads
  `comms.owners.releases.read().get(name)` -> OwnerReleaseReceipt(before,after,
  thread). Compare full process_identity, name, created_at; liveness is
  `receipt.thread.process_alive`. No JSON-string Thread or PID mirror.
- #229 / parent: history_views/supervised_cutover use Thread.process_alive and
  clear process_identity=None. Toad current Thread JSON consumes the new field.

PR comments directly hand off these APIs. Full caller closure remains required;
#232 is draft until coordinated consumers integrate. No compatibility shims.

## Stores / cutover

Thread process bindings and typed OwnerReleaseStore are runtime ownership state.
Parent must quiesce, clear old bindings/receipts and relaunch owners at cutover.
Preserve durable names/created_at/session provenance/aliases/goals/history. Do
not reset the whole registry. No runtime converter is introduced. Parent owns
quiet install/relaunch and installed Toad/ACP acceptance. No live roots changed.

## Actual local evidence

- shared-recovery-tests.log: **41 passed** (real child/grandchild retirement,
  identity refusal, reservation ordering and failed reservation reap, actual
  exec failure, cancellation, namespace escape, watchdog survives launcher
  SIGKILL, real SQLite read lock released on parent-lifeline loss; A12/S13
  guards; recovery gateway socket/SQLite admission and response tests).
- owner-runtime-smoke.log: actual candidate worker socket attaches; start is
  idempotent; restart records a new birth and retires old owner; stop retires
  replacement and preserves registry. Fresh isolated root, no provider prompt.
  First run found deleted interval import; source fixed, failure receipt kept.
- backend-adoption-tests.log: **16 passed**, actual subprocess/RPC discovery,
  persistent backend fixture reuse, queue/steer, cancellation/reap.
- installed-pi-discovery.log: actual installed Pi CLI returned **424 models**
  and configured thinking levels through candidate BoundedRun; no model call.
- windows-process-smoke.log: real Windows CPython under Wine, unchanged A12/A1/A2
  source in an isolated package (not the full app), named-job termination and
  mismatched birth refusal. Full-package import hits existing active_route.py
  fcntl dependency. Darwin implementation has not run on a Darwin host.

Local behavior evidence does not claim quiet installed acceptance or full
cross-platform app success. Old per-module mock supervisor tests were deleted;
shared OS behavior tests replace the old mechanisms, not their mocks.


## Latest caller/test closure

GoalWaits no longer takes a PID liveness callback: the snapshot's Thread owns
that decision. Goal action/recovery callers use it directly. No old probe alias
remains. Shared supervisor mock tests were deleted from test_operations,
test_start, test_restart and test_owner_release_restart. The real release tests
now exercise actual TERM, a deliberately surviving released owner, forced
retirement before replacement, voluntary exit during grace, and refusal to
escalate when birth/admission/release receipt changes after TERM.

`owner-acceptance-tests.log`: **14 passed** (includes four real owner-process
cases; do not add the four in owner-process-tests.log again). `all-guards.log`:
**11 passed**, 3037 deselected, using the exact local R0 marked-guard command.
First guard collection found an import of deleted reservation proof in obsolete
tests; those tests/import were removed. Owner fixture API mistakes were fixed
and their receipt preserved; two leftover owned fixture replacements were
identified by their test-root saved identities and retired before rerunning.
Final artifact cleanup found a third from the failed receipt fixture after pytest
rotated its directory; exact process birth plus its own AGENT_COMMS_ROOT proved
ownership before retirement. No live fixture workers or test artifacts remain.

`reattach-first-failure.log` is an actual new-session path failure at
ThreadManagement.claim_thread's obsolete pid= constructor. This is the explicit
#235 adoption dependency, not a passing ACP claim. Copernicus has the exact
constructor/replacement/full-identity scope. Parent's tests/test_private_nk_entrypoint
still mocks the removed owner-local subprocess/reservation API; its runtime
launch-pin acceptance must move to the shared child owner/real path alongside
parent cutover. S10 pi_events adoption is already present on234 (the earlier note inspected
main, not that branch). S12 NativeOwnerLoss remains an owned dependency.

The Windows CPython/Wine test download, isolated package, and Wine prefix
(421 MB) were removed after recording results. The probe script is retained as
windows_process_probe.py. No live owners or installation were changed.


## S9/S10 requested shared seams are now implemented

`BoundedRun.require_inherited_deadline()` probes kernel pidfd support before
intent. `BoundedRun.run_inherited(tuple_argv, deadline=absolute_monotonic,
pass_fds=(authority_fd, *retained_fds), input=request, cwd/env)` runs synchronously,
returns ChildResult, and preserves the caller as the native executable's actual
parent. Existing gate retains descriptors; independent WatchDeadlineCommand
inherits only pidfd, arms before exec, and survives owner SIGKILL. This is for
the existing trusted non-forking helper contract, not a PID namespace substitute.
Timeout returns TimedOutOutcome; setup/transport OS errors propagate for the
caller's UNKNOWN handling. S9 retains its business 30-second limit and exact
external authority/native request formats. No CompletedProcess adapter.

A12 stop now joins the retained retirement task through repeated cancellation,
then propagates cancellation. Bounded captured runs also join the stream task.
`cancellation-inherited-deadline-tests.log`: **19 passed** across all current
A12 behaviors/guards, including repeated cancellation for run and session,
actual direct parent+inherited descriptor execution, and real flock retention
through launcher SIGKILL until the independent deadline releases the child.
This supersedes the earlier A12-only counts, which overlap.
`s10-reproduction-fixed.log`: S10's exact reproduction now reports caller
finished while child alive: False; no extra cleanup needed for retirement.
S9/S10 have direct PR-comment API handoffs. Their own source deletions remain
their assigned work. Parent integrates the coupled branches and activates.
