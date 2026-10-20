# Phase 7: dispatch checked, ready work

Owner: pi-cn-phase7. Rebased onto main `65f58db93` under the direct conflict instruction.
P270's merged-order and installed-proof integration belongs to pi-connector (#2036).

## Implemented ownership

`PlanPhase.task` declares a `PhaseTask`. `UnspecifiedPhaseTask` stays visible
but cannot invent an order or start a unit. `CheckedPhaseTask` carries the
existing `PendingProblemOrder`, including its source, check, edits and brief.
Production full-phase task briefs are still separate work; the dispatch proof
is not their completion.

`PlanIdle.enforce` calls that task. The existing `UnownedWheelSpin` is the sole
owner selecting unowned rows: pair eligible idle units first, then allocate a
fresh destination only for the remaining unowned rows. Its stall watcher still
uses idle pairing only, so no spare leaves the old holder and strikes untouched.
`UnownedWork` and `WorkDispatch.reissue` are deleted.

`WorkDispatch.publish` owns assignment publication for both destination lifetimes.
Fresh dispatch preflights storage, allocates its checkout and stopped native
registration, publishes, then calls `OwnerLifecycle.start`. `ReusedWorkDispatch`
publishes to an existing unit without allocating or starting another process.
The existing `WheelSpin` base builds the reissued action and runs both rule kinds;
the watcher retains its own assigner identity and revision-checked strike/release.
Failed or uncertain startup remains retained, never replayed or adopted.

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
- `UnownedWheelSpin.watch/after_stall` remain the connector's entrypoints.
  `tests/test_stall_watcher_strike.py` remains a consumer, including refusal,
  blocked orders, no spare and root-release behavior.
- Only `UnownedWheelSpin.violations` selects unowned rows. There is no second
  rule or generic standing-rule import of dispatch; that import would recreate
  the storage/rule/thread cycle.
- The current source census is `P256-stall-owner-before.txt`: both retained and
  main implementations, tests, tools and Toad were parsed through `Package.load`.
  The first Toad path was absent; the corrected owned Toad checkout was appended.
  Dynamic receiver resolution is not a behavioral proof.
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

## Source checks

Source growth alone is not a defect under the owner's corrected ruling. No new
budget-only or formatting-only commit is used for this reconciliation. Both
production roots still need their structural checks; the checker and baselines
are not modified here.

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
