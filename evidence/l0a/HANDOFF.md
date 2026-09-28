# L0A: current registry and goal persistence

Owner branch `refactor/round2-l0a-loaders`; worktree `~/wt/comms-refactor2-l0a-20260928`.

## Implemented

- `goals.py`: remove flat-record loader and pause-event recovery loader. Remove the flat display projection too: storage and agent-comms payloads both use `FieldCodec.encode/decode(Goal)` with typed state.
- `registry_document.py`: remove owner-epoch and missing-counter migration, stale-alias recovery, manual disk encoding, and generation-presence flag. Decode the current document once and enforce creation identity, owner/admission, presence and alias invariants.
- `registry_store.py`: persist the existing typed document via `FieldCodec`; missing files yield an empty document, existing invalid documents fail. Private durability guard still verifies before cache access.
- `registration.py`: remove all branches that admitted unmarked documents or upgraded them on first turn.
- `goal_history.py`: use the same current typed Goal record, preserving reconciliation and durable history behavior.
- `thread_management.py`: remove directory permission repair, thread purge/result carrier, and old bus-marker rename call. Non-private imported session directories fail instead of being silently repaired. Archive retains declarations/history. Model default parameter has its actual meaning.
- `cli_commands.py`: delete the public purge command.
- Delete tests of removed conversion/purge mechanisms; retain incarnation/goal/authority behavior. One scoped R0 guard covers all seven owned production files, with zero exceptions.

## Stored format and activation classification

`registry.json` now encodes the RegistryDocument declaration directly: `threads`, `statuses`, `last_seen`, `aliases`, `owners`, `admissions`. Thread records use `FieldCodec`, including typed `goal.state` and declared ActiveTurn fields. Counters are GenerationCounter records. The registry's declarations, creation identities, session paths, goal and name reservations must survive. Runtime ownership/admission/active-turn state is reset under parent's quiet cutover; no attempted input is replayed. Resetting a whole registry would lose durable identity: do not do that.

`goal_history.sqlite3` is durable. Its schema and revision ordering are unchanged, but `before_goal`/`after_goal` payloads must be rewritten once to typed Goal records before this reader is activated. Paused records without attribution need the saved pause evidence and must never silently resume. Parent owns this conversion and the archived/snapshot registry conversions, with private guards/manifests refreshed consistently. No converter was installed in production source.

Read-only inventory at implementation start: live root 104 threads, all have creation timestamps; 18 flat goals, 2 history entries. Saved ~/.agent-comms root 104 threads, all timestamps present; 20 flat goals, 456 history entries, five paused goals lacking pause_source. See saved-format-inventory.json. These are observations, not permission to mutate those roots.

`imported_sessions` is external Pi session data; none was changed. The observed saved directory is already 0700. A directory with incorrect permissions requires explicit install repair; the runtime no longer repairs it.

## Crossings and remaining acceptance

- **Parent L0B #229** owns bus/ReadLedger integration and durable one-shot cutover, stopped-owner activation, installed behavior, and deletion of used cutover tools. Our ThreadManagement no longer calls bus.rename_thread/remove_thread or the public purge guard.
- **Darwin #231** owns `tools.py`: delete `_delete` and the `comms_delete` declaration; update tool catalog/context tests and `tests/test_channels.py`'s retired purge setup. Core CLI removal is here. No edits made to Darwin's files.
- **Parent Toad** owns `src/toad/app.py`: remove `comms_delete` progress/dispatch logic and the direct `comms.threads.delete` call. Also migrate five `Goal.from_wire` calls in `toad/acp/agent.py` and `screens/goal_details.py` to `FieldCodec.decode(Goal, payload)`, since core now emits typed Goal records. No Toad tree edited here.
- **Lovelace #232** owns `threads.py`: RegistryDocument no longer calls Thread.from_registry. Delete the now-unused Thread.from_registry, registry_created_at and session_created_at methods with their old date recovery. Current decoding is FieldCodec on Thread; its ProcessIdentity changes will participate automatically. No threads.py edits here.

This branch is not independently installable before stored data cutover and these caller removals. Full L0A completion remains open until the coordinated changes and installed acceptance land.

## Local evidence

- 77 tests passed in 4.64s: real subprocess CLI, fresh-process registry/goal history, pause behavior, registration lifecycle, incarnation fencing, import external contracts, turn release, source guard.
- Additional registration shard: 32 passed (including the two shared new tests); the guard initially identified the purge call and now passes after full entrypoint deletion.
- Wider goal/relationship/DM shard: 66 passed, 3 failed. The retained DM rebind tests still encounter the existing old bus reader's pending-history counts after removing physical purge from their setup. Keep their assertions; rerun with parent's actual ReadLedger change, not an assertion relaxation.
- No live restart, install or live state mutation. Full suite/integration and parent cutover remain pending. Slow CI deferred.

## Typed protocol and packaged acceptance

- Core Goal payloads now carry the same typed state used in persistence. `GoalHistoryEntry.to_wire` also emits typed nested goals. External ACP framing is unchanged; the agent-comms-owned metadata changes in the paired Toad release.
- 69 focused goal/runtime tests pass, including current runtime snapshots and retry notifications.
- All marked guards: 5 passed; ratchet delta: type identity -2, long booleans 0, string subscripts -2. Required GitHub run 36440516230 passed at source 70fb790.
- Built wheel installed only in this worktree's disposable target. Fresh real CLI processes registered an isolated owner, set a goal, reread it and its history, stopped and archived it, then proved identical retained history. Package import was from that candidate wheel, not the live runtime. Receipt: installed-cli.json. No mock/provider/live-owner action involved.
