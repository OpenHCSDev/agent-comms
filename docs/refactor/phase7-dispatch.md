# Phase 7: dispatch checked, ready work

Owner: pi-cn-phase7. Integration base: origin/main `6a0f5e931`.
Shared task capability repair: pi-plan-claims #1928, checkpoint `1220a914f`.

## Implemented ownership

`PlanPhase.task` declares a `PhaseTask`. `UnspecifiedPhaseTask` stays visible
but cannot invent an order or start a unit. `CheckedPhaseTask` carries the
existing `PendingProblemOrder`, including its source, check, edits and brief.
Production full-phase task briefs are still separate work; the dispatch proof
is not their completion.

`PlanIdle.enforce` calls that task. `UnownedWork` replaces the old
`UnownedWheelSpin` path, so checked released work does not require an already
idle unit. Both use `WorkDispatch`: storage preflight, isolated checkout,
stopped native declaration, canonical goal/order publication, then existing
`OwnerLifecycle.start`. Publication precedes execution. Failed or uncertain
startup state remains retained, never automatically replayed or adopted.

The task's exact problem identity and accepted installed order determine its
completion. Other closed subtasks, withdrawn orders and merely delivered
orders do not complete it. Finite positive deadline intervals are required.
The existing assignment trace requirement still applies on transfer.

## Source relationships

The before/after census used refactor-audit's existing `Package.load` parser
over Core, tools and Toad production roots. The retained census reports zero
parse omissions. NRA's parser was unavailable in the shared environment;
that limitation was not treated as a successful NRA scan.

- `CommsStandingRulesTool.apply` and `commander.rotate.broken_rules` use
  `StandingRule.broken/enforce`; no second rotation entrypoint was added.
- `PlanPhase` and `PlanIdle` consume the dependency-light `PhaseTask` owner.
- `CheckedPhaseTask` stays with dispatch; no compatibility re-export exists.
- `UnownedWheelSpin` has no remaining declaration or consumer.
- `UnownedWork` belongs to the existing commander rule discovery package.
  Importing dispatch in generic `standing_rules` instead created the real
  storage/rule/thread import cycle; moving the one member removed that cycle.
- `ProblemOrder` owns refusal of unsupported reissue operations.
- `Registration` and `OwnerLifecycle` retain identity and process ownership;
  no launch codec, second task store or copied fork history was introduced.
- Toad does not import the changed owner classes. Dynamic family discovery
  and provider execution are runtime facts, not proved by the AST census.

The CLI proof uses declaration-owned commands, reply states and the existing
`FieldCodec`. Observation uses `WireWatch`, not sleep polling. The configured
commander is read from its owner rather than compared with another literal.

## Real native result

Latest fresh run: `P256-native-20261019-1`, exit 0, source driver `643131ca3`.
No fake transport, provider, events, order or reply was used.

- Unit: `unit-p1792441766503708626-r0`
- Configured model: `openai-codex/gpt-6.1-sol`
- Problem: `P1792441766503708626`; order: `ee1774f76fcb`
- Actual first reply: `ROTATION_REPLY_c9124a08ed18496fbf81c8a2ddff4ec3`
- Second rotation duplicated neither unit nor order.
- The actual reply passed the canonical order check. The unit stopped only
  after its native turn ended; original native history remains intact.

The later typed receipt decode was checked against that original saved order;
the read-only reply check also returned 1. This establishes source dispatch
through the configured native process, not candidate installation or full
production-phase completion. No new provider input is needed to retain it.

## Source-line allocation

Both roots' structural findings were repaired before allocating feature lines.
Against the integration base the new dispatch/task implementation adds 238 and
removes 37 production lines (net 201). Its real native proof adds 287 tool lines.
The batch therefore declares `Src-Budget: 488`, the exact combined new-feature
allocation, through the ratchet's existing commit-trailer mechanism. Neither
the checker, baseline, thresholds nor structural findings are changed. This is
the same allocation mechanism used by the installed install-gate feature.

## Retained evidence and limits

Owned scratch: `/home/ts/.cache/agent-scratch/pi-cn-phase7/`. Keep the original
native run immutable, including its private bus, order, reply and native history.
Before/after source observations and bounded test/ratchet results are in the
same owned directory. Disposable test/build output may be retired after borrower
checks; user sessions and uncertain inputs are not disposable output.

Earlier native proof `run-20261019-control2` also passed. The initial control1
attempt was refused before spawn for an unavailable prepared native package;
it was not replayed or hidden. No candidate install or PR merge was performed.

The old root staging patch is removed: it described a superseded task API.
The full ControlPlane brief belongs to PR #1932's body and shared task owners,
not a second inactive implementation copied into the repository.
