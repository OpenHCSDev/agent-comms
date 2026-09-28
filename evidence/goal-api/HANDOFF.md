# Goal API deletion + A8 goal-store and PR145 boundary closure

Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/166

Implementation candidate: `5333361` on `codex/refactor-goal-api-20260928`.
Owned persistent tree: `/home/ts/wt/comms-refactor-goal-api-20260928`.
Merged parent main `829ce55` before migrating the actual TurnRunner/OwnedTurn consumers.
Parent owns paired Toad migration, serial integration, installation and actual-provider acceptance.

## What is deleted

- `GoalAction.from_legacy` and string/options dispatch from `Comms.update_goal`.
- The custom legacy `Goal` constructor, hidden `_state`, `GoalStateProjection`, `GoalState.from_legacy`, state `load` constructors, Goal status/block_reason/pause_source/active/toggle forwarding accessors and `GoalPauseSource` enum.
- The dict-to-typed conversion in `GoalMentionSource.__post_init__`; external decoding now owns nested mention bindings.
- `GoalPauseEvents.snapshot`, `GoalWaits.snapshot`, unused events argument to `GoalPauseEvents.for_goal`, and generic unknown-field filtering in `GoalWaits._decode`.
- RuntimeRequest extension-key filtering, `accepts_declared_fields`, every `invalid_payload_message` override and obsolete revision-purpose wording metadata. One canonical FieldCodec decode now rejects unknown input; real goal text validation remains.

See `deleted-surface.json`. No replacement compatibility facade or parallel registry is introduced.

## Current contracts — paired Toad rollout required

1. **Saved/ACP/socket goal JSON is still flat**: text, id, progress, revision, reported_turn, mention_source, status, block_reason, pause_source. Decode with `Goal.from_wire(payload)`; encode with `goal.to_wire()`. Do not construct `Goal(**payload)` or serialize it with `asdict` for the external protocol. Thread, goal history, tools, session metadata and ACP events now use this boundary.
2. Internal goals are normal frozen dataclasses with one public `state: GoalState`. Construct `Goal(text, id, state=PausedGoal(OwnerPause()))`; immutable transitions use `replace(goal, state=...)`. No string status constructor remains.
3. Use `goal.state.active`, `goal.state.reason`, `goal.state.pause_source` (a PauseSource or None), and `goal.state.declared_name` for display. `goal.state.toggle` is a GoalAction class or None; `goal.state.toggle_label` is the UI label. For sending the existing UI RPC, use `action.declared_name` after testing action is not None. `goal.summary` still owns its formatted summary.
4. `Comms.update_goal(name, command: GoalAction, *, actor=RuntimeInvocable, owner_store=None)` accepts an actual command, e.g. `PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id, expected_goal=goal))`, with `actor=OwnerInvocable` for authenticated owner actions. Preconditions live on the command; no owner_action/model_report flags or loose keyword options.
5. **Toad's actual UI Agent.update_goal/set_goal/retry_goal and socket request fields stay external protocol APIs.** Do not rewrite these receivers into core component calls. Core turn orchestration stays under `agent.turns` from PR161/162. Toad methods that return Goal must use Goal.from_wire.
6. `GoalPauseEvent.source` is a PauseSource object. Its wire JSON source is still a string through `event.to_wire()/from_wire()`. Use the typed source's behavior directly. GoalPauseEvents/GoalWaits consumers use `.read()`.
7. RuntimeRequest accepts the canonical `action` envelope only. Unknown extensions and a competing `kind` field are rejected; missing/type-invalid fields expose canonical decoder details rather than old wording.

`toad-caller-inventory.json` records actual source references at the locally known Toad main SHA. Current production callers are in `src/toad/acp/agent.py`, `screens/goal_details.py`, `widgets/conversation.py`, and `widgets/goal_bar.py`. Parent should also migrate direct core Comms fixtures to typed GoalAction commands. UI Agent methods keep their existing protocol signatures.

## Saved data and execution authority

No schema rewrite, live restart, or historical replay was performed. Goal registry/history remain flat on disk. Existing unattributed paused rows still preserve owner stop; matching saved pause audit records retain their original provenance. Historical blocked rows without an explicit reason remain readable as reason unavailable. Mention bindings decode through A2.

Older waits retain absent target turn generations/report witnesses as absent. No new witness is synthesized; UNKNOWN, owner pause, failed-attempt and stale-generation gates remain in force. Generic `future_field` was only a synthetic ignored-field test, replaced with rejection that preserves bytes. Read-only audit of `/home/ts/.agent-comms/goal_waits.json` found four actual rows and no undeclared keys. The two earlier supplied private roots no longer have wait files. See saved-wait-audit.json; no original file was changed.

## Local evidence

All pytest runs use absolute `PYTHONPATH=<this tree>/src`, the existing historical-views `.test-venv/bin/python`, `-o addopts=''`, and shell bounds of 60/165 seconds. CI is deferred.

- `final-goal-acp-tests.txt`: 378 passed, 1 skipped. Goal state/action extension experiments, saved registry/history round trips, mentions, owner pause, standby/UNKNOWN, ABA/CAS, real local sockets, runtime commands and actual ACP event consumers.
- `final-component-tests.txt`: 182 passed, 1 skipped. ACP goal admission, original-input authority, dispositions, TurnRunner, SessionLifecycle, InputDrain, delivery history, runtime, declarations, Toad core contract, tools and supervised cutover.
- `native-goal-tests.txt`: 47 passed, 1 failed. Both actual native/local fake-provider queued-summary cases passed (normal and foreign ingress); the one failure was an old `replace(goal, status=...)` fixture in compaction-gate coverage. That caller is fixed; `compaction-gate-tests.txt` is the full gate rerun, 19 passed. No configured-provider/network model calls.
- `delivery-consumer-tests.txt`: 78 passed. Wake, goal UI creation, private n/k ACP delivery, output, current input delivery and runtime goal editing.
- Earlier failed batches are retained to show the fixture migrations, not presented as green.
- Ruff I/F and git diff --check pass.
- `nra-final.json`: exact_compact_global, 79 detectors, zero omitted. Two findings concern existing ThreadStatus lifecycle consumers and existing ViewPredicate/SavedView serializer duplication, outside this assigned surface. No new competing Goal authority is reported. This is structural analysis plus local executed tests; authored boundary/receiver migrations are not claimed as NRA native-equivalence proofs.

## Integration and reversibility

Merge this full branch with paired Toad source changes before replacing the installed runtime. No dependency pin or native bundle changes are included. Build in parent's owned integration tree and exercise mounted Toad goal create/edit/pause/resume/retry/history against the canonical socket contract. Existing saved flat files can be read by the current deployed generation during the coordinated cutover; there is no destructive data migration to undo. Roll back paired packages together if needed; never replay interrupted/UNKNOWN inputs to validate.

No core caller blocker remains. Actual paired Toad acceptance, configured-provider acceptance and deployment belong to parent; this source PR is not itself a deployment claim.
