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
not complete the phase. pi-plan-claims is integrating PlanPhase.done and A2's
corrected dependencies separately.

Source trace also exposed an authority bypass: releasing an order skipped
Assignment.require_trace on its next transfer. Todo.assigned now applies the
same trace requirement to the released assignment history. Auto-dispatch passes
no fabricated source trace. After two owners, missing real tracing stops that
transfer rather than pretending the old order's source owner was newly read.
