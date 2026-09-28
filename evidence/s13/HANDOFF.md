# S13: child supervision

Base: main #225, `15a4d00`. Canonical plan: draft #227,
`docs/refactor/round2/S13-child-supervision.md`.

## Ownership and adoption API

Branch `refactor/round2-s13-child-process`; worktree
`/home/ts/wt/comms-refactor2-s13-20260928`.

Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/232

Initial writes: `src/agent_comms/child_process.py`, `tests/test_child_process.py`,
`tests/test_child_process_guards.py`.
S13 next owns `backend.py`, `owner_lifecycle.py`, `recovery_gateway.py` and their
actual consumers/tests. No edits to S9 compaction or S10 native files. Thread's
ProcessIdentity field change must be agreed with parent before editing.

- `ProcessIdentity(pid, start_time)`: strict dataclass through existing A2;
  `capture(pid)` is the OS boundary, `alive()` checks the recorded incarnation.
- `AttachedChild.start(tuple_argv, cwd=..., env=...)`: async launch with
  stdin/stdout/stderr streams; `wait()` / `stop()` return ChildOutcome.
- `BoundedRun.run(tuple_argv, timeout=..., input=..., cwd=..., env=...)`:
  async captured `ChildResult(outcome, stdout, stderr)`; timeout retires whole
  process group, including a grandchild which ignores TERM after leader exit.
- `DetachedProcess.launch(tuple_argv, ...)` / `attach(ProcessIdentity)`:
  launch records identity; liveness and signaling reject mismatched incarnation.
- `Platform` on A1, capability `PidfdHandles`, lifted libc/Python exact pidfd
  operations and deadline selector from `compaction_child_watchdog`.

- `NamespacedChild.start(tuple_argv, deadline=<absolute monotonic>, cwd=..., env=...)`:
  typed PID1 readiness, release only after independent pidfd watchdog arms,
  exact namespace retirement including a descendant escaping the process group.
- `ChildCommand` declaration family owns typed namespace-init/watchdog child
  entrypoints; A2 decodes once. No second manually maintained command roster.
- `GracefulStopOutcome` / `ForcedStopOutcome` distinguish the stop stages even
  when the leader exits but its grandchild resists TERM.

Consumers must wait for the foundation to land; no local copies or bare-PID
adapters. Linux behavior is exercised locally. Darwin birth-time/group support
uses libproc's actual proc_bsdinfo ABI; not executed on this Linux host. Windows
support, consumer migration/deletion and installed acceptance remain required.
This draft is NOT surface completion.

### Thread declaration handoff requested from parent

Proposed single authority: replace stored `Thread.pid` with optional
`process_identity: ProcessIdentity` (None for non-executing threads), derive the
numeric PID projection from it where actual external OS/ACP paths need it.
Do not add a parallel PID field or decode pre-cutover runtime records. Parent
must agree file ownership before S13 edits threads.py or caller files shared
with current batches; Thread registration/projection and Toad callers must be
closed with the corresponding runtime reset/relaunch. No edits there yet.

S13 next migration files remain backend.py, owner_lifecycle.py and
recovery_gateway.py. No native_pi.py, selected_pi_child_deadline.py, compaction,
channel/catalog or parent bus/cutover edits.

## Stores and cutover

This initial code changes no on-disk store and no live process. The upcoming
Thread process identity is runtime owner state: reset/relaunch at the parent's
quiet cutover. Durable wire/goal/history is untouched. No converter is added.
S9/S10 own deletion of implementations lifted from their modules when adopting
A12. Parent owns installation and real current-owner quiet restart acceptance.

## Evidence

`PYTHONPATH=src TMPDIR=$PWD/.artifacts timeout 40
/home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest
-o addopts='' -n0 tests/test_child_process.py -q`: **6 passed, 8.01s**.

These use real local OS children, not mocked spawn/signal calls: bounded and
attached child + TERM-resistant grandchild retirement, identity mismatch refusal
while actual target remains alive, ordinary exit codes, cancellation cleanup,
and one declaration-family extension. No paid providers or live owners touched.
Cross-platform, affected installed Pi/ACP and snapshot acceptance are still
required. Full scope AST guards become green with caller migration; none is
claimed for this first independent foundation commit. CI is deferred apart from
the separate R0 guard work.

Initial source adds exceed deletes because this is the isolated A12 foundation;
caller deletion follows on this same surface before it can complete.


## Extended local evidence

`foundation-tests.log`: **11 passed, 13.58s** (9 real-process/family behaviors,
2 A12 guards). New behaviors cover real namespace containment, a descendant
escaping its group, watchdog surviving launcher's SIGKILL, and actual exec
failure as a typed failure. Group guards require own session on each spawn;
detached control guard forbids a bare-PID API.

After moving child entrypoints to A1/A2 and bounding reap time,
`typed-entry-tests.log`: **3 passed, 8 deselected, 5.51s**, explicitly exercising
namespace/deadline/owner-death and exec-failure paths. This is not 14 distinct
tests. Scope-wide caller guards remain open until consumer migration completes.

Darwin ABI source: https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/sys/proc_info.h
No Darwin/Windows runtime success is claimed from Linux tests.
