# Commander, native in Core: consolidated plan

**Repository:** agent-comms, `main` at `fb9c23665` (2026-10-18). **Builds on:** `docs/design/commander-in-core.md` (the record plane) and `docs/p107-commander-core.md` (what shipped). **Adds:** the control plane the design leaves out, and a parity gate before any script is deleted.

## Where things stand

1. **Orders have two owners.** Core already has native order tools: `CommsTodoCreateTool` (a problem with its owner, checked order and deadline), `CommsOrderTool` (reserves a Todo assignment and publishes its Goal with a deadline and commander), `CommsOrderDoneTool` (verifies the order's check on its delivered commit before completing the Goal), `CommsProblemCloseTool` (closes with installed evidence, only with no open order) and `CommsTodoTool` (typed assignment, strike, release and state operations; the Todo carries `strike_count`). The commander scripts keep the same facts in private files under `CommanderConfig.state`: `problems.json`, `deadlines.json`, `closed.jsonl`, `sent.jsonl`, `strikes.jsonl`, `edits.jsonl`, `verdicts.jsonl`, `growth_seen.json`, `rules.jsonl` and a `forks/` directory.
2. **The caller has two sources.** `invoke --from THREAD` exists, and `ToolCaller.from_environment` (reading `AGENT_COMMS_THREAD`) still exists beside it.
3. **Commanded goals were merged and deferred.** #934 added commanded, uncommanded and transferred goal authors, `AcceptanceCheck`, typed goal progress and reports, delivery and todo assignment states. Commit `39290cac` removed them the same day: "Defer commander goals until retained-store conversion is ready".
4. **The design covers the record, not the control loop.** `commander-in-core.md` gives orders, verdicts, strikes, plain words, branch hygiene and the public story one owner each. It never mentions eight things the scripts do: the heartbeat, standing-rule checks, rule review, load and forking, the growth alarm, storage hygiene, the send guard and order quality. Its final step deletes the scripts.
5. **Names diverged.** The design's tool names (`comms_todo_create`, `comms_todo_assign`, ...) differ from what shipped (`CommsTodoCreateTool`, `CommsOrderTool`, `CommsOrderDoneTool`, ...). This plan uses the shipped names and updates the design to match.

## Rules for this work

- Every fact has one owner in Core. Boards, overdue lists, alarms, load, the story and dangling branches are derived on read, never stored.
- Every commander operation is one `ToolRequest`, called from any harness with `invoke --from THREAD --tool NAME`.
- Reads go through rows indexed by goal, unit or repository; nothing scans the bus.
- `FieldCodec` is the only codec; no environment variables; no compatibility readers for the old files.
- Each phase deletes what it replaces in the same change.
- No script is deleted until its capability has a native owner and the parity gate passes.

## Parity matrix

| # | Capability | Script source today | Native owner | Phase |
|---|---|---|---|---|
| 1 | Create a problem with owner, order and deadline | `order.py problem`; `problems.json` | `CommsTodoCreateTool` (exists) | 1 |
| 2 | Assign, reassign, drop an order | `order.py send`, `--reassign`, `drop`; `deadlines.json` | `CommsOrderTool`, `CommsTodoTool` assignment and release (exist) | 1 |
| 3 | Complete only on the delivered commit | `order.py done` | `CommsOrderDoneTool` (exists), returning a delivery state: merged to `main`, merged to another branch, not merged | 1, 3 |
| 4 | Close with installed evidence | `order.py close`; `closed.jsonl` | `CommsProblemCloseTool` (exists) | 1 |
| 5 | Board, overdue, silent, spinning | `order.py board`, `overdue`; `fleet-board.md` | `CommsTodoListTool` and the fleet tool, derived (exist) | 1 |
| 6 | Branch hygiene | `branches-*.txt` by hand | `CommsWorkBranchesTool` (exists) | done |
| 7 | Strikes and the strike limit | `order.py strike`; `strikes.jsonl` | `Strike` rows on the order; `StrikeLimit` declared on the commanded author; one `comms_fire` | 3 |
| 8 | Verdicts on a unit's tool calls | `judge.py ok`, `back`; `verdicts.jsonl` | `Verdict` on the order; a send-back revises the order Goal and records the strike in one write | 3 |
| 9 | Plain words in verdicts and strikes | judge and order checks | `PlainText` value type | 3 |
| 10 | Refuse soft or vague orders | `order.py` | `OrderText` value type on every order action | 3 |
| 11 | What a unit did during its order | `tool_watch.py`; `edits.jsonl`; `judge.py show` | `comms_order_work`, read from both transcript families by `tool_call_id` | 4 |
| 12 | What the commander has not judged yet | `judge.py unjudged`; `vibe_watch.py` | `comms_unjudged`: tool calls and unit messages after the last verdict, derived | 4 |
| 13 | Public story and roasts | `fleet-narrative.md`; `judge.py roast` | `comms_order_story`, `comms_fleet_story`; each send-back verdict posts its plain reason to the order's channel | 5 |
| 14 | Heartbeat: next unit every minute | `rotate.py` (60 s loop) | a scheduled commander wake owned by Core | 7 |
| 15 | Standing rules broken right now | `rotate.py broken_rules` | a `StandingRule` family, each member deriving its violations on read | 7 |
| 16 | Rule review | `rules.py`; `rules.jsonl`; a maximum age | the rules as instruction segments in the commander's turn context, with provenance | 7 |
| 17 | Load and forking | `capacity.py`: 6 units per commander, 10-minute beat age; `forks/` | `CommanderLoad` derived from open orders and the unjudged queue; forks are registered threads | 7 |
| 18 | Growth alarm | `growth_watch.py`: merges growing `src` by more than 40 net lines | a growth check derived on read from git per configured repository | 7 |
| 19 | Storage hygiene | `storage.py`: worktree limit per unit, cleanup due under 30 GB free, nothing allocates under 5 GB | `UnitStorage` derived on read from worktree paths and free disk | 7 |
| 20 | Commander reaches units only through orders | `send_guard.py` (PreToolUse hook) | stays a hook, at the harness boundary, checking against Core's order tools | 7 |
| 21 | Paths, repositories, commander identity | `CommanderConfig` from `commander.json` | Core's project and registry configuration | 8 |
| 22 | Seeing the fleet: board, strikes, queue, story, alarms | `fleet-board.md`, `order.py board`, the terminal output of `rotate.py` and `judge.py` | a commander page in Toad, rendering Core's answers through `ui_model` | 6 |

**Dropped deliberately:** old verdicts and `edits.jsonl`. They judged copies of tool calls, and any old call can be judged again from the transcripts.

## Phases

Each phase lists its changes, what it deletes, and when it is done.

### Phase 0: retained-store conversion (the blocker)

**Changes.**
- The Todo store accepts a declared tuple of tables instead of refusing any table but `todos`, so verdict and strike rows have a declared home in the same store.
- Reinstate the types `39290cac` removed (commanded, uncommanded and transferred authors, `AcceptanceCheck`, typed progress and reports, delivery, assignment states) on the converted store.

**Done when** the store holds orders, verdicts and strikes as declared tables, and the reinstated types are on `main`.

### Phase 1: one owner for orders, now

This phase removes the duplicate authority that exists today, before any new feature.

**Changes.**
- Every script command that writes `problems.json`, `deadlines.json`, `closed.jsonl` or `strikes.jsonl` calls the native tool instead: problems through `CommsTodoCreateTool`, assignment and reassignment through `CommsOrderTool` and `CommsTodoTool`, completion through `CommsOrderDoneTool`, closing through `CommsProblemCloseTool`, strikes through `CommsTodoTool`.
- `order.py board` and `overdue` read the native tools.
- Delete `ToolCaller.from_environment`; every caller identifies with `invoke --from`.

**Deletes:** the writes to those four files and the environment-variable caller.

**Done when** a day of commanding changes none of those four files, and the board shows the same orders as Core.

### Phase 2: reconcile the design with what shipped

**Changes.** Update `commander-in-core.md`: the shipped tool names, the existing native tools, and the control-plane section from this plan.

**Done when** the design and `main` describe the same system.

### Phase 3: the record plane

**Changes.**
- `PlainText`: a value type refusing text under ten words and the declared banned words, except inside quoted, backticked or identifier spans.
- `OrderText`: a value type for order bodies that refuses what `order.py` refuses today (soft or vague orders, no stated design, no check), used by every order action.
- `Verdict` on the order: an accepted verdict must quote the judged tool call and at least four words of the order; a send-back revises the order Goal and records a send-back strike in the same write.
- `Strike` on the order, with causes for send-back, missed deadline, false completion and missing evidence, and a declared caught-at stage; the commander gets strikes on the same record.
- `StrikeLimit` declared on the commanded author; `comms_fire` transfers or releases every open order of the unit with one stated reason.
- Delivery states returned by completion: merged to `main`, merged to another branch, not merged; a completion on an unlinked branch is refused.

**Deletes:** `strikes.jsonl` writes (already moved in phase 1), the strike and verdict paths in `order.py` and `judge.py`.

**Done when** a send-back, a missed deadline and a false completion each produce one record on the order, and a second strike fires the unit in one operation.

### Phase 4: the review queue

**Changes.**
- `comms_order_work`: an assignee's tool calls during the order, newest first, from both transcript families by `tool_call_id`, folding in the unfinished turn-digest branch.
- `comms_unjudged`: tool calls and unit messages since the last verdict on each open order, derived on read.

**Deletes:** `tool_watch.py`, `edits.jsonl`, `judge.py show` and `unjudged`, `vibe_watch.py`.

**Done when** the commander reviews from these two tools alone for a full session.

### Phase 5: the story

**Changes.**
- `comms_order_story`: one order's transitions, verdicts and strikes in time order, each event rendering its own plain line.
- `comms_fleet_story`: every strike and send-back, newest first.
- A send-back verdict posts its plain reason to the order's channel when it is recorded, so public examples come from the record, never from a separate roast step.

**Deletes:** `fleet-narrative.md`, `fleet-board.md` as files anyone writes, `judge.py roast`.

**Done when** both stories render from Core and the channel shows each send-back once.

### Phase 6: the commander page in Toad

Toad is a view of Core, so the commander's page is one more view over Core's answers, with no logic of its own.

**Changes.**
- **A commander model in agent-comms' `ui_model`,** served by the observation process like the sidebar model: the board, strikes, the unjudged queue, the fleet story and, once phase 7 lands, the alarms. It is computed from the native tools' answers and holds no state of its own.
- **One page in Toad, with five panes:**
  - **Board:** each unit with its open order, deadline and delivery state, overdue orders first.
  - **Strikes:** per unit, each with its cause and where it was caught; units at the limit marked.
  - **Unjudged queue:** the commander's backlog from `comms_unjudged`, which is also the load signal.
  - **Fleet story:** send-backs and strikes as a live stream, the same lines the channel shows.
  - **Alarms:** growth, storage and standing-rule violations. The pane exists from this phase and fills when phase 7 lands.
- **Two small additions elsewhere:** each unit's open order and strike count beside its thread in the sidebar, and an order's story one keypress away from its thread.
- Toad reads only through `ui_model`. It never reads the Todo store's tables or the old state directory, and it computes nothing about orders, strikes or alarms.

**Deletes:** `order.py board` as a rendering path, and any terminal rendering in `rotate.py` and `judge.py` that only displayed state.

**Done when** the page shows the real fleet from Core alone, its board matches the native list and fleet tools exactly, and the sidebar shows open orders and strikes per unit.

### Phase 7: the control plane

**Changes.**
- **Heartbeat.** A scheduled commander wake owned by Core, firing every minute with the next unit to review and the standing rules broken now. It replaces `rotate.py`'s loop and stops when the commander thread stops.
- **Standing rules.** A `StandingRule` family; each member derives its current violations on read (an order without a deadline, an overdue order, a unit over its storage limit, an unjudged queue older than its bound, and the others `rotate.py` checks today).
- **Rule review.** The commander's rules enter its turn context as instruction segments with provenance, present on every turn, which removes the need for a review log. If a review step is kept, it is a typed record on the commander thread.
- **Load and forking.** `CommanderLoad` derived from open orders and the unjudged queue per commander. Over the limit (six units today), the commander forks through the existing fork operation, and forks are ordinary registered threads.
- **Growth alarm.** A growth check derived on read from git for each configured repository: any merge to `main` that grows the source root by more than the limit (40 net lines today) appears as a fleet signal, and a verdict accepting such a delivery must state why the growth is necessary.
- **Storage hygiene.** `UnitStorage` derived on read from each unit's worktree paths and the free disk: over the worktree limit, under the cleanup threshold (30 GB today), or under the floor (5 GB today), at which operations that allocate are refused.
- **Send guard.** Stays a PreToolUse hook, since it belongs at the harness boundary; it checks the commander's sends against Core's order tools instead of the scripts.

**Deletes:** `rotate.py`, `capacity.py` and `forks/`, `growth_watch.py` and `growth_seen.json`, `storage.py`, `rules.py` and `rules.jsonl`; `send_guard.py` is rewritten against Core.

**Done when** a commander runs a full session with no script process running, and every signal the scripts raised appears from Core.

### Phase 8: cutover

**Changes.**
- A one-shot move, outside `src`, run once and deleted in the change that lands it: open problems become Todos, open deadlines become assignments, strikes become strike rows. Rows whose cause cannot be named exactly are listed for the commander, never guessed.
- `CommanderConfig` becomes Core project and registry configuration.

**Deletes:** the `commander` package's scripts and private stores, and the state directory.

**Done when** the parity gate passes, the move has run, and nothing reads or writes the old state directory.

### Phase 9: the skill

**Changes.** The `comms-commander` skill uses only Core tools; its rule files become the declared instruction files phase 7 loads into the commander's context.

**Done when** the skill names no script.

## Parity gate (before phase 8)

1. **Every row of the matrix** names a native owner that is on `main`.
2. **A shadow run:** the native commander and the scripts run side by side for a working session on the real fleet, with the commander page as the Core side of the comparison. Open orders, overdue orders, strikes, the unjudged queue, growth alarms, storage alarms and standing-rule violations are compared at the end; every difference is explained or fixed.
3. **No private state written:** during the shadow run's native half, the old state directory does not change.

## Order and dependencies

| Phase | Depends on |
|---|---|
| 0 retained-store conversion | none |
| 1 one owner for orders | none (uses tools that exist) |
| 2 design reconciled | 1 |
| 3 record plane | 0 |
| 4 review queue | 3 |
| 5 story | 4 |
| 6 commander page in Toad | 5 (its alarms pane fills with 7) |
| 7 control plane | 3 (standing rules read orders and strikes) |
| 8 cutover | 4, 5, 6, 7 and the parity gate |
| 9 skill | 8 |

Phases 0 and 1 can start at once and run in parallel. Phase 1 matters most: it ends the duplicate ownership of orders that exists on `main` today.
