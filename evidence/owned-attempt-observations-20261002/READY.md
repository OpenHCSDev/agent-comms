# Owned observations and child retirement: installed scoped checkpoint

Production checkpoint `91095469` based on merged534 `457b1047`:
**21 lines deleted / 46 added, two production files.** Existing owners only.

- DurableTurn's declared acknowledgment/context/model/tool/compaction observations
  use existing Coordination.run_async. One fresh SQLite connection is created and
  closed by that operation's worker. The SAME original DurableTurn owns the fence;
  its context-manager borrows only worker AttemptStore for synchronous MroDispatch
  and restores caller resource on success, error and cancellation. Nested phase
  consumption stays within the same operation. Unhandled events acquire nothing.
  No event queue, phase mirror, additional DurableTurn or caller connection transfer.
- DurableTurn.finish records Settling plus observed backend completion/death/progress
  in one existing fenced AttemptStore transaction. The former second Settling write
  is deleted. Failure/UNKNOWN/publication paths retain their original guards.
- ChildProcess's one physical retirement driver executes its unchanged guarded
  stop plan. Async custody joins the driver in the existing executor before pipe
  release/reap on the event loop; synchronous custody uses the same driver in its
  caller's guarded thread. All birth/group scans, signals, grace/force deadlines
  and absence proofs remain. No cached process membership or altered timeout.

## Final bounded checks

Normal wheel and declared15 dependencies: `.artifacts/runtime-owned-observation536`.
All311 production files match, exact hashes/directURL in `installed-source-proof.json`.
Native assets unchanged; this Python-only check builds no native package. Installed
`agent-comms --help` passed without public registration/native input.

Six installed controls passed in total: two passed in the initial invocation;
four passed in8.08s after creating the missing owned scratch parent directory.
The initial four setup errors did not run those bodies; original output is retained.
No passed checks were repeated. Risks covered:

1. Real SQLite EX blocker cannot block its event-loop release callback; cancelling
   the joined observation still retains the exact committed fence and caller access.
   Native phase projection derives from NativePhaseChanged, replacing obsolete
   test expectations that durable projection independently counted raw tools.
   Completed settlement adds exactly one revision; declared completion then succeeds.
2. Actual attached child plus TERM-defiant grandchild retire; every original group
   scan runs away from the event loop (instrumentation delegates actual OS checks).
3. Repeated cancellation joins both real tree shapes before returning.
4. Birth mismatch refuses signals while the real child survives until its owner
   cleans it. This control passed on the initial invocation.
5. Synchronous parent pipe custody closes after a real exception, covering the
   shared driver. This control passed on the initial invocation.

Six recorded tree identities are absent (see actual receipt count; no public owner
was stopped). Small disposable pytest outputs reside under the owned scratch
`/home/ts/.cache/agent-scratch/mendel-owned-observation536-20261002`; normal prefix,
wheel and receipts remain in this persistent WT. No provider call or native input.
Home4.5GiB headroom prevented heavy allocations; this cached wheel/prefix/check
batch used small existing dependencies, no agent fleet or native build.

## Acceptance limit

Original385 finish-to-return/publication intervals remain unallocated, not measured
function or lock durations. This removes synchronous event-loop work at its existing
resource owners; it does NOT prove actual public38–49s replies fixed. Original385,
UNKNOWN and private530 evidence are untouched. Parent owns the next paired install
and actual configured-channel journey; no additional provider probe was sent.

Before declaration/consumer AST output is adjacent. Production roots311, tools53,
tests357 parsed without omissions. Nominal/dotted references include ambiguous
receivers; dynamic resolution is not inferred. Native JavaScript/SDK/OS are not
parsed by the Python AST and remain unchanged. Arendt granted these method scopes;
Einstein's compaction/source-policy and535 evaluation remain disjoint.
