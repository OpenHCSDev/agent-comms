# Native goal-bar failures at the assembled pair

Core: 7b4c03223b716a5625e358b6b297169d378b12ae.
Toad: 6587aee50bc73af398d16a33c6807c33bdeec038.
Original result: 94 passed / 6 failed; this contribution owns the three native goal-bar cases only.
Full original log: /home/ts/.cache/agent-scratch/comms-friction/p23-qualified-env-consumers-toad.log.

## Usable readiness cut

Apply `readiness.plan.json` against the exact Toad source. One native PatchTarget operation on the existing goal_on_real_owner fixture; the native simulation only selects that helper source. It waits for the actual registry goal projection and the existing control group's mounted lifetime before yielding. No assertion or product source changes. Native simulation exit 0; no test run or shared-tree edit.

Trace: tests/support/native.py attached_app yields after agent.session.settled and connected. This attests ACP attachment, not arrival of the independent GoalObservation (session_observation.py) or ComposedWhenShown.show mounting children (native_style.py). GoalBar.watch_goal_display performs show, and GoalBar.shown_children supplies its actual controls and pulse. tests/support/native_steps.py goal_on_real_owner currently yields directly at that attachment boundary. The two failed tests immediately query children; the passing standby test explicitly waits for the standby header before querying its pulse. Readiness belongs in the arranging fixture, preserving all three consumer procedures and expected observations.

Original failing roots: /tmp/toad-test-pu6ixtdk (HistoryInteraction query), /tmp/toad-test-dluj81c5 (StandbyPulse query). Original test outcomes remain retained there; this plan is not a repaired-behavior claim.

## Third failure: actual running successor, no product fix proposed

Root /tmp/toad-test-c_ifxbx7 shows first turn de546e2ea5224ec5bd9e7ed104ccd60d (generation 1) failing without output. The next turn 654c0e8366904216ab142f7b4695865d (generation 2) finishes its native requests. The next turn 88891d080fdf449b85191ab6d457e265 is generation 3 in model_wait; its request trace ends at dispatch before headers. The fixture itself calls provider.hold_next_request while answering the productive generation, deliberately holding this subsequent autonomous request. The failure screen renders Goal Running while that turn is active.

The goal ledger after teardown retains a verified_turn resolution naming the productive generation-2 turn, and a later failed/operator_stopped attempt from teardown of the held successor. This post-teardown ledger is not claimed to be a same-time UI snapshot. It distinguishes the successful resolution from the later still-running work that teardown stopped.

Core's original main ActiveTurn.execution returns RunningExecution when the live process matches the owner. P23's ActiveTurn.execution delegates to GoalExecutionSource.active_turn_execution with the same predicate and result. IdleTurn still asks generation.lifecycle.idle_execution. GoalDetails renders the source's details_heading and watches both goal_display and goal_execution. Running is therefore backed by a different live execution fact, not evidence that the verified turn failed to settle. Changing the label to Active would falsify that fact.

Maintenance is NOT used as a test fix: OwnerStop.terminal reads a maintenance hold as OperatorStopped before goal-account finish, so taking a hold before productive settlement could erase the successful resolution the test must prove. Stopping the managed owner also removes the live owner that OwnerGoalRead requires. No such synthetic workaround is included.

The third test still needs an honest test arrangement or requirement correction: preserve separate proof of the failed-ledger resolution, the new running lease, and the correct controls. Its Active assertion is left unchanged by this contribution. No weaker label, new UI conditional, internal-state mutation, provider rerun or competing suite was introduced.

## Superseding third-case arrangement

Friction corrected the requirement after the source trace: do not force an idle owner or alter scheduler authority. `successor.plan.json` adds the exact independent proofs instead. The original controlled provider captures the actual `ActiveTurn` serving its productive tool request. The failed snapshot supplies the actual failed attempt ID. After the held successor enters, the test requires `read_attempt(...).resolved_as(ResolvedAttempt(), DecidedResolution(VerifiedTurnDecision(productive.id)))`, a different current lease and generation with the matching live process, the unchanged goal and a real `get_goal_snapshot()` equal to RunningExecution for that goal. The lease must remain unchanged across the snapshot read. The already-open details must match that exact available goal/execution and render the heading derived from that owner snapshot; Retry, Pause and the draft assertions remain. There is no Active-or-Running alternative and no product status conditional.

This supersedes only the earlier statement that the third arrangement was unresolved; the failed-state trace above remains the evidence for the correction. The next request stays held by the original provider gate; its deliberate disconnect at fixture close is declared. A final helper patch requires the actual available owner goal rather than an unavailable display retaining the same snapshot.

Three native operations across the two Toad helper/test sources simulated with exit 0. No product/shared-source edit or native/app test run. The helper readiness prefix is already applied in friction's assembly; apply only this successor suffix with normal native reproof.

## Assembled behavior result

Friction applied the successor suffix with full shared-context reproof at Toad c0020780a3f8a67550575d193eb8fa9222241b9d, paired with Core f2f589d762f730d39665ace2011fdfdb93f55749. Its targeted native goal-bar/objective run collected and passed 10 cases in 159.51s, exit 0. Read receipts: /home/ts/.cache/agent-scratch/comms-friction/p23-repaired-consumers-toad.log and p23-repaired-consumers-toad.exit. Both original native files using the arranging fixture were included. No extra run was launched by this contribution. This qualifies the applied goal-bar repair in that assembled pair; installation and the separate real Standby-owner acceptance are not asserted here.
