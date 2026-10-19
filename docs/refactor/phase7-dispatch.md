# Phase 7: start checked, ready work

Owner: pi-cn-phase7. Base: origin/main 011084fc5; plan dependency change 89f53149a is integrated from pi-plan-claims.

## First slice

`PlanIdle` reports ready phases; rotation only prints its strings. Existing
`UnownedWheelSpin` requires an already-idle unit, leaving unowned work untouched
when none exists. A never-ordered phase has no checked task declaration. Inventing
an acceptance command from a phase name would bypass the order's actual owner.

Put optional checked tasks on PlanPhase, using PendingProblemOrder rather than a
second order schema. Unspecified tasks remain visible and allocate nothing.
Checked tasks and released orders share native unit startup: storage preflight,
isolated worktree, stopped thread declaration, native order publication, then the
existing explicit owner start. Publication precedes execution. Keep failed or
uncertain startup state; do not replay or remove it automatically.

`StandingRule.enforce`, already called by CommsStandingRulesTool and rotation,
is the entrypoint. No rotation-function edit is necessary. Delete the competing
UnownedWheelSpin path; preserve FlatWheelSpin's separate reassignment behavior.

## Coordination and verification limits

PR #1912 changes rotate.main/broken_rules/owed_orders, plus commander context.
Leave those functions alone. #1756 changes order/judge pattern IDs, which this
slice does not touch. No Toad file changes are needed.

pi-plan-claims owns plan_phases.py, and explicitly permits only the task imports
and PlanPhase.task declaration in this slice. It retains shared claims and plan
parent/dependency ownership. Checked production task briefs are not yet supplied.

The current instruction forbids launching another agent. Do not run the requested
scratch Pi startup proof under that restriction. Source checks cannot replace the
required unit/order/first-reply proof.

Resource helper /home/ts/bin/agent-resource-check is absent. Manual observation:
4.1 GiB available RAM; 26 GiB disk available. Keep checks small and serial. Scratch
owner: pi-cn-phase7; purpose: bounded final checks; path:
/home/ts/.cache/agent-scratch/pi-cn-phase7. No saved user or Codex sessions belong
in that disposable directory.

## Source evidence

NRA import is currently blocked by DuplicateRegistryKeyError in its detector
registration against the installed metaclass-registry. Use refactor-audit's
existing Package parser for the bounded AST census instead, recording omissions.
AST results establish source sites, not live binding or runtime equivalence.

Before edit: refactor-audit Package.load over all 379 production Python modules,
zero parse omissions. Enumerated classes/bases and matching references across the
whole package; imports, writes, comparisons and decisions in the rule/phase
owners. Semantic consumers: tools.CommsStandingRulesTool.apply ->
StandingRule.broken/enforce; commander.rotate.broken_rules invokes that tool;
PlanIdle owns phase readiness attention; commander.standing.UnownedWheelSpin
owned the competing idle-unit-only path; GoalAction subclasses and TodoStore
own checked assignment; Registration and OwnerLifecycle own process identity
and launch; CheckoutStorageEffect owns allocation policy. No dependency code is
changed. Dynamic DeclaredFamily discovery and provider execution remain runtime
obligations, not statically proven bindings.

Implemented: PhaseTask.start/done, with an unspecified member that cannot invent
an order and a checked member carrying PendingProblemOrder. PlanIdle delegates to
its phase task. UnownedWork replaces UnownedWheelSpin and starts a dedicated unit.
WorkDispatch checks storage, fetches main, creates the owned worktree, registers
a stopped native thread, publishes its checked order, then calls OwnerLifecycle
start. No fork copies, new launch codec or second task/order store.

Completion uses the explicit task problem and ProblemOrder.accepted: only
InstalledProblemOrder accepts; a withdrawn/folded/merely delivered problem does
not complete the phase. pi-plan-claims' a44d1be1e completion/A2 change is merged
normally into this branch. Its borrowed module was reconciled with the current
finite-deadline and exact-task identity checks.

Source trace also exposed an authority bypass: releasing an order skipped
Assignment.require_trace on its next transfer. Todo.assigned now applies the
same trace requirement to the released assignment history. Auto-dispatch passes
no fabricated source trace. After two owners, missing real tracing stops that
transfer rather than pretending the old order's source owner was newly read.

## Earlier sanity checkpoint

After edit: refactor-audit parsed 380 Core production modules and 283 Toad
modules (ba57101e), zero parse omissions. UnownedWheelSpin has no declaration
or consumers. PlanPhase.done and PlanIdle consume PhaseTask; task.done is the
only order.accepted consumer. Toad has no imports of the changed owner classes.
Dynamic discovery and provider execution remain unproved.

Source imports and git diff --check pass. Final bounded background run used
`timeout 300`, Python `-X faulthandler`, `-o addopts=''`, one process and an owned
basetemp: 12 tests passed in 10.64 seconds (work_dispatch, plan_phases and affected
PlanIdle/wheel-spin cases). pytest-timeout is absent; external timeout applies.

The broader check had 16 passes and six failures: four board cases read missing
checkout /home/ts/wt/worker--P109, one compares changing memory snapshots, and my
new trace test used a banned word. The last is fixed and passes. This is not a
passing full standing-rule suite. pi-cn-phase7 owns the missing-checkout rotation
risk: reproduce with the board self-check cases; acceptance must preserve the
remote PR fact without a local checkout. Default eight-worker/coverage settings
were needless overhead for that bounded run; final checks were serial.

At that checkpoint no agent launch, provider input, installation or PR merge had
occurred. The scratch native first-reply proof below supersedes that limitation.
Production full-phase declarations are staged with pi-plan-claims. Unordered rows
without a checked order still cannot be dispatched by inventing their check.

The full ControlPlanePhase brief draft was moved into PR #1932's body at the
owner's request. The owner explicitly authorized rotation to start the proof unit
on a scratch bus; no helper delegation or live-bus proof writes are authorized.
The real proof runner is tools/prove_commander_dispatch.py. Its new scratch
root, configuration, native history and receipts are retained under
/home/ts/.cache/agent-scratch/pi-cn-phase7/run-*/. Owner: pi-cn-phase7; purpose:
first native reply and duplicate-dispatch acceptance. Native unit checkouts use
the existing StorageRule's /home/ts/wt/<unit>--P<n> allocation. No auth is copied.
The development checkout reuses an unchanged prepared native package by reflink
copy at stack/.pi-native-a0f57a811b47862f (192 MiB apparent size). Its manifest
matches main and VerifiedNativePackage.acquire accepts it. The native verifier
correctly refused a symlink; no gate was disabled and no live package changed.

## Real rotation/native proof

Run: run-20261019-control2, exit 0. No fake Comms, provider, events or replies.
`commander.rotate.broken_rules` called the real native standing-rule tool, whose
PlanIdle enforcement created this registered unit and order:

- Unit: unit-p1792433571161170326-r0
- Model: openai-codex/gpt-6.1-sol; reserved worker PID 1206539
- Problem: P1792433571161170326
- Order: 0f96e2831ff1
- First actual unit reply: ROTATION_REPLY_62821ddec7434083b6f618e65798a080

Second rotation created neither another unit nor another order. The actual reply
passed the order's Core check; its order is DeliveredProblemOrder, pending=False.
The worker was stopped only after its native turn ended. No proof writes touched
the live bus. Receipt, order, reply, diagnostics and original native session are
retained in the named scratch run. The initial control1 run was refused before
spawn for a missing prepared package and is retained separately, not replayed.

Dispatcher source was bf9fae2f1; the proof runner was then committed as c723313ed.
The native worker used the existing main-checkout editable Core (observed clean
at 8b27e02aa) and unchanged pinned Pi a0f57a811b47862f. This is a real source-driver
and configured installed-native process proof, not a claim that the candidate
branch was installed or that all of phase 7 is complete.

The requested full task() factories and method migration are staged in
phase7-task-declarations.patch and sent to pi-plan-claims (owner request 1895,
handoff 1946). That owner reports stale self-claims preventing its shared-file
edits; no denied edit was bypassed. Its phase/claim changes must be integrated
normally. Missing-checkout progress reads overlap the recently changed
ProgressSource in #1935; pi-cn-phase3 has the reproducer and implementation
handoff, so no competing ProgressSource change is made here.
