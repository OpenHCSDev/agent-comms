# Commander, native in Core: consolidated plan

**Repository:** agent-comms, `main` at `fb9c23665` (2026-10-18). **Builds on:** `docs/design/commander-in-core.md` (the record plane) and `docs/p107-commander-core.md` (what shipped). **Adds:** the control plane the design leaves out, and a parity gate before any script is deleted. **Companion:** `docs/agents/commander-game.md` holds the measurements and design behind the alerts, the commander page, momentum predictions and measured load (step zero, phases 3, 6 and 7 cite it).

## Where things stand

1. **Orders have two owners.** Core already has native order tools: `CommsTodoCreateTool` (a problem with its owner, checked order and deadline), `CommsOrderTool` (reserves a Todo assignment and publishes its Goal with a deadline and commander), `CommsOrderDoneTool` (verifies the order's check on its delivered commit before completing the Goal), `CommsProblemCloseTool` (closes with installed evidence, only with no open order) and `CommsTodoTool` (typed assignment, strike, release and state operations; the Todo carries `strike_count`). The commander scripts keep the same facts in private files under `CommanderConfig.state`: `problems.json`, `deadlines.json`, `closed.jsonl`, `sent.jsonl`, `strikes.jsonl`, `edits.jsonl`, `verdicts.jsonl`, `growth_seen.json`, `rules.jsonl` and a `forks/` directory.
2. **The caller has two sources.** `invoke --from THREAD` exists, and `ToolCaller.from_environment` (reading `AGENT_COMMS_THREAD`) still exists beside it.
3. **Commanded goals were merged and deferred.** #934 added commanded, uncommanded and transferred goal authors, `AcceptanceCheck`, typed goal progress and reports, delivery and todo assignment states. Commit `39290cac` removed them the same day: "Defer commander goals until retained-store conversion is ready".
4. **The design covers the record, not the control loop.** `commander-in-core.md` gives orders, verdicts, strikes, plain words, branch hygiene and the public story one owner each. It never mentions eight things the scripts do: the heartbeat, standing-rule checks, rule review, load and forking, the growth alarm, storage hygiene, the send guard and order quality. Its final step deletes the scripts.
5. **The live commander runs a third copy.** On Tristan's machine the loop runs scratch copies in `/home/ts/.cache/agent-scratch/rts/` with their own `problems.json`, `deadlines.json` and `strikes.jsonl`. Core's `commander/` package is not running there at all (no `commander.json` on that machine). On 2026-10-18 four rules were added only there: an order states the user-visible run that closes it (`Works when:`, 8+ words); `done` needs a file proving that run happened; moving a problem past two owners needs the commander's own trace (`--traced PATH:LINE`); wording in unit messages gets a correction, not a strike (86 wording strikes were deleted from the scratch strikes file instead of voided). Branch `rts-commander-tools` pushed a further copy under `tools/rts`.
6. **Noise pushed the commander onto cheap checks.** About 700 of 1,136 recent feed messages were units posting "Claimed" to their own thread, and OVERDUE repeated 150-170 times per missed order. Under that load the commander closed P437 on a grep without starting the CLI (it broke every `agent-comms` command in the next build) and let Pi Durable pass through five owners without one real turn. Measurements: `commander-game.md`.
7. **Names diverged.** The design's tool names (`comms_todo_create`, `comms_todo_assign`, ...) differ from what shipped (`CommsTodoCreateTool`, `CommsOrderTool`, `CommsOrderDoneTool`, ...). This plan uses the shipped names and updates the design to match.
8. **One commander per machine.** `CommanderConfig.path()` is the single file `~/.config/agent-comms/commander.json`, and `install` writes the state directory as `~/.local/state/agent-comms/commander`. A second commander for another project, such as DQDock beside OpenHCS on 2026-10-18, has two options. It can overwrite the running commander's config, or it can share that commander's state, board, repositories, load count and strikes. The send guard is a global PreToolUse hook that knows only the one installed commander name, so it cannot protect a second one.

## Rules for this work

- Every fact has one owner in Core. Boards, overdue lists, alarms, load, the story and dangling branches are derived on read, never stored.
- Every commander operation is one `ToolRequest`, called from any harness with `invoke --from THREAD --tool NAME`.
- Reads go through rows indexed by goal, unit or repository; nothing scans the bus.
- `FieldCodec` is the only codec; no environment variables; no compatibility readers for the old files.
- Each phase deletes what it replaces in the same change.
- No script is deleted until its capability has a native owner and the parity gate passes.
- An order closes only when the code is shaped right and the named run works in the real app (Tristan 2026-10-18). A grep closes nothing.
- Timers drive attention. Deep problems with several past owners get a traced path and a realistic first-commit deadline instead of strike-driven reassignment, which throws context away.
- Whoever made and checked a change merges it. No handoff exists only to review, merge or rerun a check someone already ran; review is the commander reading the diff, and one recorded run counts once (Tristan 2026-10-18: P507's finished, checked fix was handed to a second agent to merge and cost a build cycle).
- A proof names the build it ran on. A run artifact carries the runtime ID and its Core and Toad revisions; a close is refused when that runtime does not contain the order's delivered commit, and source is judged at that commit, never in a checkout that may be behind (Tristan 2026-10-18: P504 was offered a screenshot from a build without the change; P529 was judged in a stale checkout).

## Parity matrix

<!-- Generated by python -m agent_comms.commander_parity; edit row declarations. -->
| # | Capability | Current commander | Native owner | Native phases |
|---|---|---|---|---|
| 1 | Create a problem with owner, order and deadline | `order.py problem`; `problems.json` | `CommsTodoCreateTool` | `one_order_owner` |
| 2 | Assign, reassign, drop an order | `order.py send`, `--reassign`, `drop`; `deadlines.json` | `CommsOrderTool`, `CommsTodoTool` assignment and release | `one_order_owner` |
| 3 | Complete only on the delivered commit | `order.py done` | `CommsOrderDoneTool`: merged to main, another branch, or not merged | `one_order_owner` |
| 4 | Close with installed evidence | `order.py close`; `closed.jsonl` | `CommsProblemCloseTool` | `one_order_owner` |
| 5 | Board, overdue, silent, spinning | `order.py board`, `overdue`; `fleet-board.md` | `CommsTodoListTool` and the fleet tool, derived | `one_order_owner` |
| 6 | Branch hygiene | `branches-*.txt` by hand | `CommsWorkBranchesTool` | `one_order_owner` |
| 7 | Strikes and the strike limit | `order.py strike`; `strikes.jsonl` | `StrikeTodo`, `ObjectiveStrike`, `ProblemGoalOrder.StrikeLimit`, `TodoStore.fire` |  |
| 8 | Verdicts on a unit's tool calls | `judge.py ok`, `back`; both invoke `CommsVerdictTool` | `CommsVerdictTool`, `SendBackVerdict`: one retained event and Goal revision |  |
| 9 | Plain words in verdicts and strikes | judge and order checks | `PlainText`, `StrikeReason` |  |
| 10 | Refuse soft or vague orders | `order.py` | `OrderText`, `CheckedProblemOrder.require_wording` |  |
| 11 | What a unit did during its order | `tool_watch.py`; `edits.jsonl`; `judge.py show` | `comms_order_work`: both transcript families by `tool_call_id` | `review_queue` |
| 12 | What the commander has not judged yet | `judge.py unjudged`; `vibe_watch.py` | `comms_unjudged`: calls and unit messages after the last verdict | `review_queue` |
| 13 | Public story and roasts | `fleet-narrative.md`; `judge.py roast` | `comms_order_story`, `comms_fleet_story`; send-back reason posted once | `story` |
| 14 | Heartbeat: next unit every minute | `rotate.py` (60 s loop) | Core-owned scheduled commander wake | `control_plane` |
| 15 | Standing rules broken right now | `rotate.py broken_rules` | `StandingRule` members derive violations on read | `control_plane` |
| 16 | Rule review | `rules.py review` prints every rule; the session hook rereads them in full | Rule instruction segments in the commander's turn context, with provenance | `control_plane` |
| 17 | Load and forking | `agent-comms-commander capacity`: load and live fork reports; ten-minute beat age | Reporting-only `commander.capacity.Load.of`; no capacity refusal or automatic fork policy | `control_plane` |
| 19 | Storage hygiene | `StandingRule.broken` storage delivery (formerly `storage.py`) | `UnitStorage.violations`, `DiskLow.violations`; live `StorageRule` thresholds | `control_plane` |
| 20 | Commander reaches units only through orders | Nothing: the PreToolUse send guard was deleted | Harness hook checking Core order tools | `control_plane` |
| 21 | Paths, repositories, commander identity | `CommanderConfig` from `commander.json` | Core project and registry configuration | `cutover` |
| 22 | Seeing the fleet: board, strikes, queue, story, alarms | `fleet-board.md`, `order.py board`, `rotate.py`, `judge.py` | Toad commander page rendering Core through `ui_model` | `commander_page` |
| 23 | An order states the user-visible run that closes it | scratch `rts/order.py send` (`Works when:`) | `ApplicationOrderRun`, `InstallTodo`, `CommsProblemCloseTool` |  |
| 24 | Reassigning past two owners needs the commander's trace | scratch `order.py send --reassign --traced` | `Assignment.require_trace`, `Todo.assigned` |  |
| 25 | Unit wording gets a correction, not a strike | scratch `vibe_watch.py` | `PlainText`, `CommsCorrectWordingTool`: correction leaves strikes unchanged |  |
| 26 | Alerts carry only decisions | none; every line interrupts | `messages.AlertKind.require_publishable`, `tools.CommsDecisionTool` | `control_plane` |
| 27 | Momentum prediction per unit, compared with the outcome | none | Prediction record and later outcome on commander page; `CommitMomentum` is a partial candidate | `commander_page` |
| 28 | Measured load and self-report | `capacity.py` unit count | `commander.capacity.Load.of` reports orders, overdue, edits and forks; self-report comparison unverified | `control_plane` |
| 29 | No handoff of finished, checked work | none; scratch transfer moved P507 after its fix was pushed and checked | `DeliveredProblemOrder.require_transfer`: the checked delivery stays with its owner |  |
| 30 | A proof is bound to the build it ran on | none; screenshots and logs are bare paths | `DeliveredProblemOrder.require_installation`, `RecordedBuildIdentity.require_installed` |  |
| 31 | An order states its intent as a deletion target | Current order command with a declared FamilyDeletion | `FamilyDeletion.checks`, `OrderDelivery.verified`: count the delivered source |  |
| 32 | Where units' time goes, classified | none; activity.log lacks durations and turn boundaries | `UnitTime`: model, build/check/test/shell, dependency/commander waits and idle; classify large waits | `commander_page`, `control_plane` |
| 33 | Order progress beside its deadline; the unit never sees a due time | `order.py board`, `overdue`, `due`, `send`; `rotate.py` | Derived `OrderProgress`; merged work draws no strike; first commit 10 min, later progress 15 min STALL; units see no due time | `one_order_owner`, `control_plane` |
| 34 | Several commanders on one machine and bus, one per project | none; a second commander.json install overwrites the first | Registry commander/project ownership; caller from --from; scoped orders, strikes, load, board, alarms and forks; raw-send guard; duplicate project commander refused | `one_order_owner`, `cutover` |

**Dropped deliberately:** old verdicts and `edits.jsonl`. They judged copies of tool calls, and any old call can be judged again from the transcripts.

## Phases

Each phase lists its changes, what it deletes, and when it is done.

### Step zero: stop the split (no code, today)

1. Delete branch `rts-commander-tools` (a copy, not a delivery) and the commander branches already merged with nothing ahead of `main`: `commander-native/phase-0`, `phase-1`, `phase-3`, `feat/p256-core-commander`, `feat/p181-commander-parity`.
2. Freeze the scratch copy on Tristan's machine: no new behaviour goes into `/home/ts/.cache/agent-scratch/rts/`; fixes that keep the live loop running are allowed until phase 8.
3. Every commander, on either machine, runs the installed `agent-comms` commander or the scratch copy, never a third copy.
4. Rows 23-25 of the matrix (the 2026-10-18 rules) are carried into phase 3, not reimplemented elsewhere.

**Done when** the branches are gone and the only commander code is `commander/` in Core plus the frozen scratch copy awaiting phase 8.

### Phase 0: retained-store conversion (the blocker)

**Changes.**
- The Todo store accepts a declared tuple of tables instead of refusing any table but `todos`, so verdict and strike rows have a declared home in the same store.
- Reinstate the types `39290cac` removed (commanded, uncommanded and transferred authors, `AcceptanceCheck`, typed progress and reports, delivery, assignment states) on the converted store.

**Done when** the store holds orders, verdicts and strikes as declared tables, and the reinstated types are on `main`.

### Phase 1: one owner for orders, now

This phase removes the duplicate authority that exists today, before any new feature.

**Changes.**
- Every script command that writes `problems.json`, `deadlines.json`, `closed.jsonl` or `strikes.jsonl` calls the native tool instead: problems through `CommsTodoCreateTool`, assignment and reassignment through `CommsOrderTool` and `CommsTodoTool`, completion through `CommsOrderDoneTool`, closing through `CommsProblemCloseTool`, strikes through `CommsTodoTool`.
- `order.py board` and `overdue` read the native tools, and only the orders whose Goal names the calling commander (row 33). A second commander's orders never appear on the first one's board.
- Delete `ToolCaller.from_environment`; every caller identifies with `invoke --from`.

**Deletes:** the writes to those four files and the environment-variable caller.

**Done when** a day of commanding changes none of those four files, and the board shows the same orders as Core.

### Phase 2: reconcile the design with what shipped

**Changes.** Update `commander-in-core.md`: the shipped tool names, the existing native tools, and the control-plane section from this plan.

**Done when** the design and `main` describe the same system.

### Phase 3: the record plane — native implementation done

The order records and their operations below own the behavior; this phase is not another pending implementation list. P360 exercised each item through real native requests and real stores on disposable copied roots under `/home/ts/.cache/agent-rehearsal/fork-boundaries-P360-*`. Its PR retains the commands and results. These copied runs establish the native behavior, not an installed-build acceptance claim; accepted installed phase delivery remains owned by the Todo record.

| Delivered behavior | Owning declaration | Copied-root result |
|---|---|---|
| Send-back is one event, one implied strike and one Goal revision | `todos.py:2535` `SendBackVerdict`; `goal_management.py:80` `change_problem` | Pass: one verdict, one strike, one Todo revision, same Goal identity |
| Missed deadline is retained once per order and cause | `todos.py:2329` `MissedDeadlineStrike`; `ObjectiveStrike.repeats` | Pass: two observations retain one strike |
| False completion is retained once for commit and operation delivery | `todos.py:163` `OrderDelivery.check_failed`; `todos.py:194` commit integration distinction | Pass: two rejected operation deliveries retain one strike; commit family-check failure also retains one |
| The declared two-strike limit releases the unit's open work in one fire operation | `goals.py` `ProblemGoalOrder.StrikeLimit`; `todos.py:3306` `TodoStore.fire`; `tools.py` `CommsFireTool` | Pass: two strikes and exactly one native fire release the assignment |
| Application orders require a closing run; mechanical orders are explicit | `todos.py:1283` `ApplicationOrderRun`; `MechanicalOrderRun` | Pass: decoding an order without its run is refused |
| Installed closing requires the close tool, matching works and an existing artifact | `todos.py:2713` `InstallTodo`; `tools.py` `CommsProblemCloseTool`; `DoneTodoState.prepare_change` | Pass: missing artifact and direct state-close refused; artifact-backed close accepted |
| Checked delivered work cannot be transferred | `todos.py:1761` `DeliveredProblemOrder.require_transfer` | Pass: native reassignment refused, stored state unchanged |
| A proof from the wrong build cannot close an order | `todos.py:1781` `require_installation`; `launch_runtime.py:136` `RecordedBuildIdentity.require_installed` | Pass: older build refused without mutation; current build accepted |
| Renaming a decision does not satisfy its deletion | `todos.py:1341` `FamilyDeletion`; `todos.py:137` `OrderDelivery.verified` | Pass: renamed class count remains one and delivery is refused; zero-family commit accepted |
| Further ownership changes require the commander's traced source | `todos.py:2062` `Assignment.require_trace`; `Todo.assigned` | Pass: third ownership change without trace refused |
| Commander words are checked; unit wording is corrected without a strike | `todos.py` `PlainText`, `OrderText`, `StrikeReason`; `tools.py` `CommsCorrectWordingTool` | Pass: short strike refused; wording correction preserves strike count |

P360 deleted the operation-delivery silent failure default and the duplicate commit-only failure writer, sharing recording through the existing delivery owner. The matrix derives from its row declarations; delivered rows no longer carry this phase as pending work. The 86 historical scratch wording strikes still belong to phase 8's explicit void carry, not deletion or a new implementation here.

### Phase 4: the review queue

**Changes.**
- `comms_order_work`: an assignee's tool calls during the order, newest first, from both transcript families by `tool_call_id`, folding in the unfinished turn-digest branch.
- `comms_unjudged`: tool calls and unit messages since the last verdict on each open order, derived on read.

**Deletes:** `tool_watch.py`, `edits.jsonl`, `judge.py show` and `unjudged`, `vibe_watch.py`.

**Done when** the commander reviews from these two tools alone for a full session.

**Revised 2026-10-21 (postmortem: 1,280 edits went unjudged while every heartbeat reported the count).** Review per tool call gated nothing and could not keep pace with fifteen units. The unit of review is a PR head commit: one verdict family (`ReviewedHeadVerdict`, todos.py:2581) posts `agent-comms/review`, which is a required check in the merge-queue ruleset, so unreviewed work cannot merge from any shell. `comms_unjudged` lists ready PRs whose head has no verdict, cleared by the verdict; `order send`/`say` refuse once one has waited 10 minutes. The tool-call history stays as evidence for judging, never as the alarm. (P433, #2469.) **Done when** the ruleset lists `agent-comms/review` and an unreviewed PR cannot merge.

### Phase 5: the story

**Changes.**
- `comms_order_story`: one order's transitions, verdicts and strikes in time order, each event rendering its own plain line.
- `comms_fleet_story`: every strike and send-back, newest first.
- A send-back verdict posts its plain reason to the order's channel when it is recorded, so public examples come from the record, never from a separate roast step.

**Deletes:** `fleet-narrative.md`, `fleet-board.md` as files anyone writes, `judge.py roast`.

**Done when** both stories render from Core and the channel shows each send-back once.

### Phase 6: the HUD (engine, screen, context feed) — revised 2026-10-21

Commanding is a high-tempo RTS. The fleet loses information because Core hands the commander raw events and long prose and leaves it to classify every unit in its head. On 2026-10-20/21 all ten units sat blocked with no alarm, a unit's alert waited about seven hours, four orders went out twice, and work was reassigned away from the unit holding it. Every one of those had a rule in prose and nothing in Core that derived it. The reread alone is about 125K characters (46K of skill, rules and indictment plus a 76K pattern catalog), injected again every 100K tokens.

The fix is one derived model with four renderers. **Core is the engine, Toad is the screen, the commander's context is the HUD feed, and the connector serves claude-ai the same frames.** Nothing computes fleet state twice.

**Engine: `CommanderHud` in `ui_model`, derived on read, holding no state but one cursor.**
- **`UnitState` family.** The rotation tick's classification moves out of SKILL prose into Core: `Unreadable` (a checkout or other unit source cannot be read; other units still get their cards), `Blocked` (goal execution blocked while holding an order), `Detached` (not receiving), `AwaitingCommander` (a question, alert or handoff with no commander answer, from `Message.answers`), `Stuck` (no tool call while holding an order), `Spinning` (send-backs or reruns with no new commit), `StalledOnNothing` (turn ended holding an order with nothing pending), `DeliveredUnclosed` (merged or installed, order open), `Moving` (fresh commit on the order's surface), `Idle` (no order). Each member derives itself from turn digests, git, goal execution and messages; owns its card line, its ping and its default action. States are direct `UnitState` members, sharing behavior through capability mixins rather than claiming to be other states. Urgency is their declaration order in the family's registry, never a rank field.
- **Pings are decisions, each once.** A unit entering an urgent state, a review timer up, a check failed, a merge, an install, a run seen working, an owner message. Keyed by unit, state and order revision, so nothing re-fires. The `StandingRule` family is the ping source; `HolderCannotRun` (with `Detached` and `Blocked` as members) and `UnitAskUnanswered` join it.
- **Frames.** `CommanderHud.frame()` is a snapshot; `frame.since(cursor)` is the delta: only changed cards and new pings, like a netcode delta. The one stored fact is each reader's cursor.
- **Kill feed:** `comms_fleet_story` plus merges and installs, newest first. **Economy bar:** free disk against floor, merge-queue depth, units busy, idle and blocked, unjudged edits, model budget where the provider reports it. **Scoreboard:** per unit, delivered and installed orders, strikes, and self-inflicted wait minutes (`UnitTime`, row 32).

**Screen: the commander page in Toad, a pure view of `CommanderHud`.**
- Top: economy bar. Left: the board, one level-0 card per unit sorted by state, colored by state, with its order, its P-numbers in plain words, last call in plain words and lines since the order. Right: the ping lane (unhandled pings, oldest first) above the kill feed. Bottom: the zoomed panel.
- Zoom: Enter expands a card to level 1 (recent turns, one line each), `d` to level 2 (diff and single calls). Every level ends with what it left out.
- Hotkeys are Core tool requests, one keystroke each: strike, send-back, correction, resume a blocked unit, approve to the queue, close with artifact, open the order editor. The page computes nothing about orders, strikes or states.
- Frame pacing: rendering reads frames only; nothing scales with history; keypress-to-paint p99 under one frame at 144 Hz.
- The sidebar shows each unit's state glyph and open order beside its thread.

**Connector: the same frames for claude-ai, the supreme commander.**
- One read tool, `comms_hud`, renders `CommanderHud.frame()` as a bounded tree: one line per plan phase (done-evidence on main, holder, its `UnitState`) and per unit card, anomalies first, each line ending with an expand id, and a closing line naming what it folded. `expand` returns one subtree (a phase's PRs and holders, a unit's recent turns, a card's diff); `since` returns the delta from the caller's cursor.
- It replaces reading `comms_threads` (about 200K characters) and `comms_todo_list` (about 780K) whole to answer "is the fleet on the plans": the tool derives that answer; it stores nothing.

**Context feed: what lands in the commander's context, tiered like a HUD.**
- **Every tool call:** new pings only, at most five lines, each with its default action. This replaces the bus heartbeat's message text; the heartbeat keeps only the job of waking an idle commander.
- **Every 8K tokens:** the frame delta (changed cards since the last injection, at most 25 lines) plus two drift tells. The tells are derived from each rule file's `drift:` line, not a hand-kept copy.
- **Every 25K tokens:** the voice.
- **Session start and compaction:** skill, indictment, rules and the pattern catalog's index (ID plus one line each). Full rung files are read when an order or roast cites them. This cuts the standing reread from about 125K characters.
- **Units:** on every wake, their own card line and their order, plus the fleet rules they already carry. No due times.

**Deletes:** `order.py board` as a rendering path, terminal rendering in `rotate.py` and `judge.py`, the heartbeat's free-text violation lists, `drift.md` (done: tells derive from rule frontmatter), and any SKILL prose a `UnitState` member now derives.

**Done when** the installed Toad page shows the live fleet from `CommanderHud` alone; on a replayed hour of the live bus, the commander's injected context contains every decision event exactly once and no repeats; a unit forced into each `UnitState` shows up on the card and in the ping lane within one frame; and every hotkey round-trips through its Core tool.

### Phase 7: the control plane

**Changes.**
- **Heartbeat.** A scheduled commander wake owned by Core, firing every minute with the next unit to review and the standing rules broken now. It replaces `rotate.py`'s loop and stops when the commander thread stops.
- **Standing rules.** A `StandingRule` family; each member derives its current violations on read (an order without a deadline, an overdue order, a unit over its storage limit, an unjudged queue older than its bound, and the others `rotate.py` checks today).
- **Rule review.** The commander's rules enter its turn context as instruction segments with provenance, present on every turn, which removes the need for a review log. If a review step is kept, it is a typed record on the commander thread.
- **Alerts carry only decisions** (phase 6's pings own the event set). The commander's wake receives a reply addressed to it, a deadline crossing, a merge, a failed check and a works-in-app close, each once. A thread's messages to itself never reach another thread's feed; overdue is a state entered once and then shown on the page; disk pressure is raised when a threshold is crossed; unjudged counts are a column, not events. Measured before: about 700 of 1,136 feed lines were self-posts and OVERDUE repeated 150-170 times per order (`commander-game.md`).
- **Load and forking.** `CommanderLoad` derived per commander from look debt (units with new activity and no current prediction, and the oldest age), judge debt (the unjudged queue and its oldest age), decision inflow, prediction calibration and context used, plus a one-word self-report (`sharp`, `stretched`, `skimming`) recorded each heartbeat and compared with the measures. Over a limit, the commander forks through the existing fork operation for the oldest debt and new orders are refused until it is back under; forks are ordinary registered threads that report on their own thread names.
- **Where the time goes.** `UnitTime` (row 32) derived from Core's turn records: per unit and fleet, minutes in model time, tool runs by declared kind (build, check, test, other shell), waiting on a named owner, waiting on the commander, and idle. Each wait over a bound asks the commander one question on its card: does this need to take this long, or is it self-inflicted (a rerun, a handoff, a broken merge, a wrong order)? The answer is recorded on the wait, and a daily rollup ranks self-inflicted minutes by cause and by who caused them, the commander included. 2026-10-18 example: about 75 of 110 minutes of the install were self-inflicted (a merge never run, a converter proven on a partial copy, a reversed order, a needless handoff, two commanders).
- **Growth alarm.** A growth check derived on read from git for each configured repository: any merge to `main` that grows the source root by more than the limit (40 net lines today) appears as a fleet signal, and a verdict accepting such a delivery must state why the growth is necessary.
- **Storage hygiene.** `UnitStorage` derived on read from each unit's worktree paths and the free disk: over the worktree limit, under the cleanup threshold (30 GB today), or under the floor (5 GB today), at which operations that allocate are refused.
- **Send guard.** Stays a PreToolUse hook, since it belongs at the harness boundary; it checks the commander's sends against Core's order tools instead of the scripts.

**Revised 2026-10-21 (postmortem).** What shipped drifted from this phase, and the drift is the defect:
- The heartbeat was built to speak only when violations changed, not "every minute". Silence then meant both calm and dead, and it was dead from 14:08 to past 15:40 UTC on 2026-10-21 because one unit's unreadable checkout failed every pass into the journal. It beats every pass; a failed pass is a bus fact; `HeartbeatSilent` is raised in the commander's own tool process; an unreadable source is that unit's own `UnitState` member, never "idle", and the pass continues for every other unit. (#2472.)
- Every `StandingRule` declares, abstractly, the action that clears it; a rule with none cannot be declared. INSTALL DUE ran on a 30-minute clock and said "due" 64 seconds after a finished install; it derives from the installed build (P431, #2475).
- Forks stayed invisible (`capacity` read 0 live forks at load 240), so several forks gave one unit three orders in minutes. Forks are registered threads as this phase already says, and unit assignment takes one lock.
- Every heartbeat line is owed, like a unit's unanswered ask. On 2026-10-21 the commander read past twelve stopped units holding orders, PLAN IDLE and HUNT DUE for over an hour, because only asks gate the order verbs. The owner is `StandingRule` (`src/agent_comms/standing_rules.py:32`): each violation is owed a disposition, and `OwedAnswer` (#2532) becomes the member whose disposition is a reply.
  - A disposition is the order, strike, release or hunt that names the line, or a deferral with a reason of 8+ plain words.
  - `DirectiveVerb.gate` (`src/agent_comms/commander/order.py:795`) refuses while any current line has none, as it does for owed asks.
  - A line that fires unchanged for 3 beats goes to the owner.
- Rule review: every rule bullet names the standing rule, refusal or check that enforces it, and `rules review` flags a bullet with none. A prose rule nothing enforces is a fact restated outside its owner; on 2026-10-21 the commander followed the voice block to the letter and skipped every attention rule.

**Deletes:** `rotate.py`, `capacity.py` and `forks/`, `growth_watch.py` and `growth_seen.json`, `storage.py`, `rules.py` and `rules.jsonl`; `send_guard.py` is rewritten against Core.

**Done when** a commander runs a full session with no script process running, every signal the scripts raised appears from Core, and replaying one recorded hour of the live feed on a copy delivers to the commander only decision events, with no duplicates, and the same hour yields a `UnitTime` rollup whose classes add up to each unit's elapsed time.

### Current control-plane work: heartbeat source identity (2026-10-23)

Before this repair, `HeartbeatSilent.violations` read the newest row in the
two-way DM conversation and then checked its sender. A commander reply after
the last beat therefore hid a silent heartbeat. `BusReader.latest_addressed`
now selects one candidate per exact service/commander alias pair through the
existing typed table query, then decodes only the newest message. `Addressed`
declares its compound sender/target/sequence index; registry provenance supplies
the aliases. Ordinary DM display scopes retain their original contract.
`CommanderHeartbeat.requested_by` derives
live eligibility and the oldest open request from `OwnerRequestStore`; the shared
silence scan derives its grace from that request and the latest actual beat.
`HeartbeatNotStarted` and `MissedHeartbeat` own their distinct evidence and wording.
A newer request, service registration, commander reply or rename cannot erase the
older debt. No parallel timestamp, ping store or scheduler is added.

Draft #2624 is stacked on the published owner-request port #2621. Eighteen
focused copied-bus/request/page/channel/source checks pass (7.54 seconds),
including the newest beat across both participants' renames and later reverse
replies. On the retained 19,189-record bus copy, an absent service with aliases
takes 76 SQLite instructions after index reconciliation versus 45,088 before;
both return no message. All exact-pair queries use the declared covering index.
Reconciliation preserves every bus sequence and both bus/address row counts;
integrity and foreign-key checks pass, and a repeated reconciliation is a no-op.
Its current unconditional projection rebuild costs 39.83 seconds even for this
index-only change; that installer/schema defect remains open with the commander.
Installed full-session/parity acceptance and the complete control-plane phase
remain open. The source census has zero parse omissions; unprojected declarations
remain visible.

### Phase 8: cutover

**Changes.**
- A one-shot move, outside `src`, run once and deleted in the change that lands it: open problems become Todos, open deadlines become assignments, strikes become strike rows. Rows whose cause cannot be named exactly are listed for the commander, never guessed.
- `CommanderConfig` becomes Core project and registry configuration. A commander is declared once, as a registered thread with its project (row 33). Every tool takes its commander from the calling thread, never from a single machine-wide file. Each commander's open orders, strikes, load and board stay separate, and the board and capacity read only that commander's orders.

**Deletes:** the `commander` package's scripts and private stores, and the state directory.

**Done when** the parity gate passes, the move has run, and nothing reads or writes the old state directory.

### Phase 9: the skill

**Changes.** The `comms-commander` skill uses only Core tools; its rule files become the declared instruction files phase 7 loads into the commander's context.

**Changes, revised 2026-10-21.** The skill wires the commander's input to the phase 6 HUD feed; "Arm two Monitors" (SKILL.md:17) and the hand-written `gh pr list` poll the commander added are deleted, so there is one board.

**Done when** the skill names no script.

## Parity gate (before phase 8)

1. **Every row of the matrix** names a native owner that is on `main`.
2. **A shadow run:** the native commander and the scripts run side by side for a working session on the real fleet, with the commander page as the Core side of the comparison. Open orders, overdue orders, strikes, the unjudged queue, growth alarms, storage alarms and standing-rule violations are compared at the end; every difference is explained or fixed.
3. **No private state written:** during the shadow run's native half, the old state directory does not change.

## Order and dependencies

The phases and what each waits for are declared once, in `src/agent_comms/plan_phases.py` (the `PlanPhase` family). An order names the phase it serves with a `Serves: <phase>` line, stored as `ProblemOrder.serves`. A phase is done when its explicitly declared full-phase checked task has accepted installed evidence; closing related subtasks does not complete the phase. Earlier delivered phases record their delivery on their declarations. Each rotation prints `PLAN IDLE` for every phase whose dependencies are done and that has no open order (`PlanIdle` in `src/agent_comms/standing_rules.py`).
