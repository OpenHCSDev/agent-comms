# S13: child supervision

Base: main #225, `15a4d00`. Canonical plan: draft #227,
`docs/refactor/round2/S13-child-supervision.md`.

## Ownership and adoption API

Branch `refactor/round2-s13-child-process`; worktree
`/home/ts/wt/comms-refactor2-s13-20260928`.

Initial writes: `src/agent_comms/child_process.py`, `tests/test_child_process.py`.
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

Consumers must wait for the foundation to land; no local copies or bare-PID
adapters. Initial implementation is Linux. Darwin/Windows, namespace handshake,
watchdog process adoption, typed termination stages and full caller migration
remain required S13 work; this draft is NOT surface completion.

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
