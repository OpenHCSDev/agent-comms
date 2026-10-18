## Result

Model-created goals now acquire their private generation before the tool returns. The separately installed candidate passed the continuous real workflow: saved history visibly opened in Toad, native model called `comms_set_goal`, model called `comms_goal` to block, the actual visible **Retry** control was clicked, a new native attempt completed the same goal.

Draft for review. **NEEDS CLAUDE: #777.** Not merged, live runtime not relinked.

## One owner and deleted decisions

`Goals` (`src/agent_comms/goal_management.py:36`) now owns the private-store handle/opening (`:45`) and original custody-fenced READY recovery (`:52`). `update_goal` (`:372`) no longer accepts `owner_store`; `GoalActionContext` no longer carries an optional store. Store availability is not actor authorization.

- `SetGoalAction.change` (`goal_actions.py:378-386`) always records the initial generation before goal publication. UI, model-tool and runtime callers all use the same action through `Goals.update_goal`.
- Deleted scheduler store-opening/handle/recovery implementation, optional-store grant gates and `owner_grant`, plus `ReplacementGoalAction`'s independent filesystem/opening branch. Replacement/clear uses the owned store to retire existing authority.
- Additional source finding: `TurnGoalAccount.tool_ended` previously created the generation late and returned early when a row already existed. Deleted that creation/skip decision. It now claims the original recorded generation through the same custody-fenced READY recovery. Duplicate events are recognized by that account's existing originated-goal custody, not by absence of a ledger row.
- Deleted account `open_store` callbacks and caller-selected store arguments from its reservation path. Scheduler, account and `OwnedTurn` consume `Comms.goals`. No compatibility aliases or forwarding wrappers.
- Existing actor, exact-owner, goal/revision and reserved/claimed-attempt fences remain. Retry remains an explicit owner action; ordinary Resume cannot authorize a failed attempt.

## Historical goals without a record

`GoalAttemptStore.require_generation` (`goal_attempts.py:662`) raises **MissingGoalGenerationError**, with: "Clear this goal and set it again. Retry cannot create a grant." It never supplies a default generation. Its RuntimeError/ValueError capabilities use Python's normal C3 MRO; the UI error boundary can present it and store callers can recognize the named refusal.

Retry/Resume leave a historical missing-row goal unchanged. Scheduler exposes the same actionable refusal when it observes one. Clear is allowed; explicitly setting again creates a fresh ID and generation. **No live historical goals or UNKNOWN inputs were migrated, cleared, resumed or replayed.**

## Source/consumer evidence

Before implementation, reused refactor-audit's `Package` parser for every class declaration/base and relevant imports, writes, calls and decisions across the production root at base `23f4699cf`: **306 modules, no parse omissions**. Final production scan: **306 modules**; final test scan: **355 modules**, both without parse omissions. The new error's actual MRO was inspected in the installed candidate. Dynamic family dispatch/CLI/native binding is not proved by AST; it is separately exercised below. No complete JS or dependency-root AST proof is claimed.

The related family traced/migrated comprises:

- `Thread.goal` / registry publication and `GoalAction`'s shared precondition/actor/publication algorithm.
- `tools.py` model set/progress/resume/edit entrypoints and `runtime_requests.py` set/update/retry ingress. These already call `update_goal`; their API no longer permits bypassing recording.
- `GoalScheduler` UI creation, Resume/Retry, autonomous scheduling.
- `ReplacementGoalAction`, `SetGoalAction`, `ActiveGoalAction`, `RetryGoalAction`.
- `OwnedTurn`, `TurnGoalAccount` reservation/origin/settlement and its borrowed `OwnedSendAdmission` permit/store resources.
- All source tests calling the deleted scheduler methods/fields or `owner_store`; fixture-side duplicate creation removed where Goals now owns it. Missing-row tests construct explicit historical registry rows rather than using the new-goal API to manufacture a defect.

Actual final search across `src/agent_comms`:

```
rg -n 'owner_store|owner_grant|check_grant|def open_goal_store|create_goal\(' src/agent_comms
src/agent_comms/goal_attempts.py:726:    def create_goal(self, goal_id: str) -> Generation:
src/agent_comms/goal_actions.py:386:        ctx.goals.open_goal_store().create_goal(goal.id)
src/agent_comms/goal_management.py:45:    def open_goal_store(self) -> GoalAttemptStore:
```

One store-opening declaration and one production goal-creation consumer. The late event-side creator and caller policy are gone.

## Checks and remaining failures

- Clean-environment focused batch: **44 passed, 21 failed, 5 setup errors**. Passing checks include model-tool creation followed by owner-socket Retry, UI creation, historical missing-row refusal, goal Resume/block-reason/lifecycle checks. Git diff check passed.
- Remaining batch failures occur in existing catalog/setup fixtures (configured model absent from their native catalog, or `agent_bin='unused'` not an executable). Representative failures from all four affected fixture families were reproduced on an untouched clean export of base `23f4699cf`: **3 failures, 1 setup error**. This is not a claim that the whole source suite passes or that every remaining failure has been individually baseline-run.
- An initial batch accidentally inherited live-route launch variables; it produced 33 passes with fixture failures. The independent-root batch explicitly removed those child-process variables. No live route/store was written by these checks.

## Installed actual UI/native acceptance

Built a wheel from a clean `git archive`, installed it into an owned isolated environment, reused the shipped Toad/dependencies and unchanged immutable Pi package `native-current-21e71cdc648ed997`. Verified all six changed production files in site-packages match final published head `ca9a10652`; no editable-source substitution, live installation change, relink or paid provider call.

Driver: `tests/goal_generation_installed_journey.py`. It reuses `NativeBackendFixture` and `LoopbackProvider`; only localhost responses are controlled. Uses the **manifest-approved compiled comms extension**, not a loose source path or weakened import fence.

**PASS:** saved SDK history visibly opened -> actual native `comms_set_goal` tool through installed CLI -> native `comms_goal` blocked report -> visible Toad Retry click -> original owner socket -> fresh native generation -> model completed the same goal. Generation advanced from 1 to 2. Five localhost provider requests, three saved user inputs (seed, initial prompt, fresh Retry continuation). Original saved history prefix preserved. All native children retired.

Two initial verification setups were refused before prompt admission by the immutable extension-import boundary (zero provider requests). A later isolated setup incorrectly used `--no-tools`; its loopback provider failed before producing a tool response and left a started/unconfirmed input. That original session, wire, disposition and diagnostic are preserved, **not replayed**. The successful check used a separate SDK-authored run and distinctly named input with tools enabled; no original failure was hidden.

## Retained paths / next step

Owner: **doctrine-segment**, root `/home/ts/wt/core-doctrine-segment/.scratch/goal-generation`.

- `installed`, `wheels`: candidate and reusable wheel retained for review/relink handoff.
- `installed-tools-journey/native`: successful saved-source/state receipt.
- `installed-manifest-journey/native`: started/unconfirmed failed verification input and diagnostic; preserve it.
- Concise source census/results and before/after visible goal screenshots retained for review. Closed source exports and disposable unit-test outputs can be retired after borrower checks.

Await reviewer/census, no merge authorization assumed. After authorized merge, author builds the clean-export runtime within the hour; coach relinks. The current live wheel still has the optional parameter and is unfixed.
