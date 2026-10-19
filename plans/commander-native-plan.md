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
| 23 | An order states the user-visible run that closes it | scratch `rts/order.py send` (`Works when:`) | `OrderText` requires it; `CommsProblemCloseTool.works` is checked against it | 3 |
| 24 | Reassigning past two owners needs the commander's trace | scratch `rts/order.py send --reassign --traced` | the transfer operation reads owners from the Todo's assignment history and refuses without a traced source location | 3 |
| 25 | Unit wording gets a correction, not a strike | scratch `vibe_watch.py` | `PlainText` applies to the commander's own verdicts and strikes; unit wording never becomes a strike | 3 |
| 26 | Alerts carry only decisions | none (today every line interrupts) | the commander's wake receives decision events only, each once | 7 |
| 27 | Momentum prediction per unit, compared with the outcome | none | a prediction record on the order, shown on the commander page beside what happened | 6 |
| 28 | Measured load and self-report | `capacity.py` unit count | `CommanderLoad`: look debt, judge debt, inflow, calibration, context, plus a one-word self-report calibrated against them | 7 |
| 29 | No handoff of finished, checked work | none (scratch `order.py send` moved P507 after its fix was pushed and checked) | the transfer operation refuses an order whose current owner has a delivered commit with its check passing; that owner merges it, and the check's recorded run is reused, never rerun by another unit | 3 |
| 30 | A proof is bound to the build it ran on | none (screenshots and logs are bare file paths) | the run record written by the build check (`tools/build_runtime.py`) carries runtime ID and Core/Toad revisions; `CommsProblemCloseTool` refuses when that runtime does not contain the order's delivered commit | 3 |
| 31 | An order states its intent as a deletion target | none (orders name a line, so units fix the line: P520 swapped `is_set()` for `except ValueError` around every caller) | the order declares the decision family being deleted, its owner, and a family-wide count from the existing NRA/refactor-audit scanners; completion runs that count on the delivered commit and refuses unless it is zero with no new site of the same kind | 3 |
| 32 | Where units' time goes, classified | none (`activity.log` has call times but no durations or turn boundaries) | Core's turn records carry each call's start and end and each turn's start reason; `UnitTime` derives per unit and fleet the minutes in model, tool runs (build, check, test, shell), waiting on another owner, waiting on the commander, and idle; the commander marks each large wait necessary or self-inflicted | 6, 7 |
| 33 | Order progress beside its deadline; the unit never sees a due time | `order.py board`, `overdue`, `due`, `send`; `rotate.py` (before: a due time and "Delivered" only, so merged work drew the strike threat) | `OrderProgress` in Core (`todos.py`), derived on read from the unit's checkout and the repository's PRs, never stored: no commit, committed not pushed, pushed with no PR, open PR, merged; `CommsTodoListTool` returns it per held order. Merged work draws no strike; no first commit within 10 minutes, or no new commit, push or PR change for 15 minutes, is a STALL with the progress line. `CommsOrderTool` sends the unit no due time: the deadline stays the commander's fire line | 1, 7 |

**Dropped deliberately:** old verdicts and `edits.jsonl`. They judged copies of tool calls, and any old call can be judged again from the transcripts.

## Reconciled gaps (phase 2 source check)

**Baseline:** `origin/main` at `011084fc5`, checked 2026-10-19. The earlier
"Where things stand" and matrix script-source cells are the 2026-10-18 starting
inventory, not a fresh observation of the live machine. In particular,
`ToolCaller.from_environment` is gone (`src/agent_comms/tool_caller.py:26`), native
verdict/review/control tools exist (`src/agent_comms/tools.py:729`,
`src/agent_comms/tools.py:738`, `src/agent_comms/tools.py:811`), and order completion
leaves the unit's Goal unchanged (`src/agent_comms/tools.py:857`). The actual CLI
verb is `tool`, not `invoke` (`src/agent_comms/tools.py:1325`). Use
`agent-comms tool --from THREAD --tool NAME --arguments JSON` for the rules above.

The [reconciled design](../docs/design/commander-in-core.md) records shipped behaviour
phase by phase. These rows track its remaining gaps against this plan; they do not
create a second phase/dependency registry. The owner column names the phase/type
responsible, not an assignment to a new worker. `plan_phases.py` remains the sole
phase declaration. This section does not modify PR #1587's multi-commander additions,
its phase-1 board paragraph, or its phase-8 configuration paragraph.

| Gap | Existing parity rows / phase | Source boundary on main | Work still required and its owner |
|---|---|---|---|
| G1 | 9, 10, 23, 31 / phase 3 | `PlainText` has a one-word minimum; ten words belong to `StrikeReason` (`src/agent_comms/todos.py:698`, `src/agent_comms/todos.py:746`, `src/agent_comms/todos.py:753`). `CheckedProblemOrder` validates typed fields, while `order.send` still parses/checks additional order requirements (`src/agent_comms/todos.py:1137`, `src/agent_comms/commander/order.py:180`). Deletion is optional (`src/agent_comms/todos.py:1024`). | Put the required order-quality behaviour on the existing order/text/run/deletion types and remove the script-only decisions. Reconcile the planned ten-word/identifier policy with the actual types explicitly. A deletion count must cover the whole named family, including renamed sites; merely storing a command is not that proof. |
| G2 | 7, 25 / phase 3 | `StrikeTodo` stores a reason, not a declared cause/caught stage (`src/agent_comms/todos.py:1622`). The limit is declared at `src/agent_comms/goals.py:519`; `TodoStore.fire` is an explicit operation (`src/agent_comms/todos.py:2358`). Late commit strikes exist at `src/agent_comms/todos.py:137`, but verification failures raise before delivery (`src/agent_comms/todos.py:1883`). | Finish strike causes, caught stages, commander mistakes and limit handling in the record-plane owners. Prove a second strike releases all affected open orders through the native operation; a script message saying "reassign" is not automatic firing. Preserve wording corrections and strike voids. |
| G3 | 8 / phase 3 | Native verdict application checks a tool-call ID is text; accepted verdicts quote the order (`src/agent_comms/todos.py:1681`, `src/agent_comms/todos.py:1707`). Actual diff-quote/read-code checks still live in `VerdictVerb.run` (`src/agent_comms/commander/judge.py:162`). | Have the existing verdict/transcript owners check that the cited original call belongs to the order and supports the required quote. Do not treat a fabricated nonempty call ID as a reviewed original. Remove the replaced judge-only decisions. |
| G4 | 3, 6 / phase 3 | `CommitDelivery.require_source` checks ancestry and origin containment (`src/agent_comms/todos.py:156`); `MergedClaim` also checks origin containment (`src/agent_comms/progress_claims.py:44`). `comms_order_done` returns `TodoChanged` (`src/agent_comms/tools.py:867`), while branch linkage is a separate read (`src/agent_comms/tools.py:968`). | Derive/return the actual landing state (main, another branch, not merged) through the delivery owner and enforce the promised branch PR/issue requirement there. A pushed commit is not necessarily merged to main. |
| G5 | 4, 23, 29, 30 / phase 3 | Delivered transfer refusal and quoted closing-run checks exist (`src/agent_comms/todos.py:1302`, `src/agent_comms/todos.py:972`). `InstallTodo.closed` checks only that the artifact file exists (`src/agent_comms/todos.py:1923`); `WorkingClaim` does likewise (`src/agent_comms/progress_claims.py:79`). | Bind the existing run evidence to runtime ID, Core/Toad revisions and containment of the delivered commit; reject an artifact from the wrong build. Keep native delivery/close evidence requirements consistent with `ApplicationOrderRun`, not stronger only in the script. Reuse the checked result instead of handing finished work to another owner. |
| G6 | 33 / phases 1, 7 | `CheckedProblemOrder.instructions` still emits a `Deadline:` line to the unit (`src/agent_comms/todos.py:1221`). | The order-instruction owner must keep the deadline on the commander side while preserving the canonical deadline/progress used for attention. Remove the contradictory unit-facing rendering; do not add a second due-time field. |
| G7 | 11, 12 / phase 4 | Native work and unjudged tools exist (`src/agent_comms/tools.py:940`, `src/agent_comms/tools.py:738`). `OrderRead.edits_from_turns` excludes already judged edit IDs (`src/agent_comms/turn_digests.py:120`), not a queue of all calls/messages since the last verdict. The native-file watcher remains (`src/agent_comms/commander/tool_watch.py:66`). | Extend the existing transcript/review owners for the required unit-message/all-call review semantics, then delete the replaced watcher/show/alarm paths and demonstrate a full native review session. Do not rebuild the transcript records in private files. |
| G8 | 13 / phase 5 | `Todo` holds the records (`src/agent_comms/todos.py:1958`), but `order.on_record` separately appends/posts and `BackVerb.report` roasts after the verdict (`src/agent_comms/commander/order.py:414`, `src/agent_comms/commander/judge.py:262`). | Native order/fleet stories derive from those records; native send-back recording owns once-only public posting. Delete the written narrative/board and separate roast mechanism. A native send-back must not silently omit the public story. |
| G9 | 22, 27 / phase 6 | Core's UI observation boundary and thread rows exist (`src/agent_comms/ui_model/observation.py:71`, `src/agent_comms/ui_model/sidebar.py:41`), but the commander board is still a written Markdown rendering (`src/agent_comms/commander/order.py:389`). | The commander-page owner builds the Core model, its observation path, Toad panes/sidebar details and prediction records/outcome display. Verify real fleet data, not a fixture page. The absence search below covers the missing Core model; it is not a Toad acceptance run. |
| G10 | 14, 26 / phase 7 | `rotate.main` prints/sleeps and remembers its last timestamp only in process (`src/agent_comms/commander/rotate.py:26`). Undated rules return on every read (`src/agent_comms/standing_rules.py:50`). | Core owns scheduled commander wakes and their retirement, with durable once-only delivery of the declared decision events. Verify restart/stop and the recorded-hour feed comparison; timestamp filtering of repeated script output is not that mechanism. |
| G11 | 17, 28 / phase 7 | `capacity.Load` counts open/overdue/unjudged work; fork liveness is `.beat` files (`src/agent_comms/commander/capacity.py:24`, `src/agent_comms/commander/capacity.py:36`). The adapter enforces overload (`src/agent_comms/commander/order.py:762`). | The native load owner derives look/judge debt, inflow, calibration and context, records the calibrated self-report, uses registered fork threads and refuses excess new orders natively. Delete beat-file liveness and adapter-only enforcement. |
| G12 | 15, 16, 18, 19 / phase 7 | Standing-rule enforcement, typed review storage, growth and storage rules already exist (`src/agent_comms/tools.py:811`, `src/agent_comms/rule_reviews.py:131`, `src/agent_comms/commander/standing.py:180`, `src/agent_comms/storage_rules.py:61`). Rule selection still reads installed script configuration (`src/agent_comms/commander/rules.py:42`), as does growth (`src/agent_comms/commander/standing.py:226`). | Complete these existing owners, not replacement families: move rule/context and repository configuration into Core, finish native review enforcement and growth-verdict justification, and remove script dependencies. Preserve current storage enforcement. `comms_standing_rules` can strike/reassign (`src/agent_comms/commander/standing.py:120`); do not advertise it as a harmless read. |
| G13 | 27, 32 / phases 6, 7 | `OrderCall` contains ID/name/digest/state and `TurnDigest` contains events (`src/agent_comms/transcript_events.py:429`); no classified elapsed-time or prediction model is declared in the searched production root. | Existing turn/order owners supply model/tool/wait/idle accounting, prediction comparison and judgments of large waits. The page derives the rollups; the recorded-hour classes must sum to elapsed time. Do not infer elapsed minutes from a call count. |
| G14 | Rules: indexed reads / phases 3, 4, 7 | Peer acceptance walks newest wire messages (`src/agent_comms/todos.py:1718`). `TurnsSince` admits all turns since an order (`src/agent_comms/turn_digests.py:73`); growth reads a 12-hour git window (`src/agent_comms/commander/standing.py:185`). | Repair bounded lookup/preparation at the existing record/read owners. Remove the concrete full-feed lookup and bound review work to requested results without losing review coverage. Do not repeat the blanket constant-cost claim until these paths meet it. |
| G15 | 20, 21; PR #1587 / phases 1, 8 | Todo queries are commander-scoped (`src/agent_comms/todos.py:2268`), but adapter config is one machine-wide file (`src/agent_comms/commander/config.py:71`). The hook binds that configured commander (`src/agent_comms/commander/send_guard.py:155`). | Project/registry owners provide the project-scoped commander identity/configuration and the hook consumes it. Preserve both commanders' records and reject conflicting commander registration as specified by #1587. This row adds evidence, not a competing implementation or a duplicate numbered parity row. |
| G16 | Parity gate; phases 1, 4, 7, 8, 9 | Scripts/entrypoint, file rendering, fork directories and script-based skill remain (`pyproject.toml:60`, `src/agent_comms/commander/order.py:389`, `src/agent_comms/commander/config.py:83`, `src/agent_comms/skills/comms-commander/SKILL.md:10`). The skill requests `growth-watch` (`src/agent_comms/skills/comms-commander/SKILL.md:19`), but the package has no such module. | The existing phase owners finish their native paths; the cutover owner records the real-fleet shadow/no-private-write session, moves scratch facts once (ambiguous causes listed, wording strikes voided), removes obsolete scripts/config/state, and the skill owner rewrites instructions for native tools. Branch deletion, scratch freeze and migration are operational checks, not facts proved by a source marker. |
| G17 | Phase completion / phase 2 and plan-claims owner | `MergedPhase.done` unconditionally returns true; `DesignReconciledPhase` names an older delivery (`src/agent_comms/plan_phases.py:50`, `src/agent_comms/plan_phases.py:72`). Other phases derive closure from serving Todos (`src/agent_comms/plan_phases.py:36`). | `pi-plan-claims` owns reconciliation of recorded phase completion with its evidence. This phase-2 document repair neither changes that module nor treats an earlier hardcoded marker as proof of today's reconciliation or runtime parity. |
| G18 | 24 / phase 3 | `Assignment.require_trace` has the two-owner check (`src/agent_comms/todos.py:1496`), but `Todo.assigned` calls it only if the current assignment exists (`src/agent_comms/todos.py:2047`). `proposed_assignment` preserves released-owner history without invoking it (`src/agent_comms/todos.py:2040`). | The existing assignment owner must apply the trace requirement to the complete owner history, including release-then-assign. Source reproducer: two distinct owners, release the current assignment, then assign a third owner without a trace. Acceptance: the same native assignment operation refuses that path without a real traced location. |

For the absent story/load/time declarations and commander UI model in G8–G13,
recheck the pinned tree with these searches, then read any matching declarations:

```sh
git grep -nE 'class .*Story|CommsOrderStory|CommsFleetStory|CommanderLoad|UnitTime' \
  011084fc5 -- src/agent_comms
git grep -nEi 'commander|prediction|strike' \
  011084fc5 -- src/agent_comms/ui_model
git ls-tree -r --name-only 011084fc5 src/agent_comms/commander
```

No fixture tests, suite deltas or installed-runtime claims close these rows.

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

- The 2026-10-18 rules (matrix rows 23-25 and 29-31): `OrderText` requires the user-visible run that closes the order (8+ words) unless the order is declared mechanical, and closing checks against it; the transfer of a problem with two or more past owners refuses without the commander's traced `PATH:LINE`; unit wording never becomes a strike; an order whose owner already delivered a checked commit cannot be transferred, its owner merges; a close's run artifact names its runtime, and a runtime without the delivered commit is refused; an order states the decision family it deletes and completion refuses while the family count is above zero or a renamed site of the same kind appears. The 86 wording strikes in the scratch file are carried as voids in phase 8, never deleted.

**Done when** a send-back, a missed deadline and a false completion each produce one record on the order, a second strike fires the unit in one operation, and on a copied root an order without a works line is refused while one with it closes only through `CommsProblemCloseTool` with an artifact, and transferring an order with a delivered, checked commit is refused, a close citing a run from a build without the delivered commit is refused, and a delivery that renames the decision instead of deleting it (the P520 case) is refused by its family count.

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
- **Unit cards and levels of detail.** Level 0 is one line per unit: open orders and due times, whether the files edited match the files the order named, commits and lines since the order, the mix of recent calls by tool kind, the unit's last claim beside the git fact, whether anything is new since the last look, and the unit's largest wait since its order with its class (row 32). Level 1 expands one unit into its recent turns, one line each. Level 2 expands a turn or file into the diff and single calls. The same levels are commands for any harness (`look`, `look UNIT`, `look UNIT diff FILE`). Sketch with real data: `commander-game.md`, "The screen" and "Distilled looking".
- **Momentum.** Every look at a unit with new activity ends in a recorded prediction: would I do what it did and why, will it land on time given its pace, and what steer changes its course. The next look shows the previous call beside what happened, so the commander's accuracy is visible. `commander-game.md`, "Momentum".
- Toad reads only through `ui_model`. It never reads the Todo store's tables or the old state directory, and it computes nothing about orders, strikes or alarms.

**Deletes:** `order.py board` as a rendering path, and any terminal rendering in `rotate.py` and `judge.py` that only displayed state.

**Done when** the page shows the real fleet from Core alone, every unit has a card with its latest prediction and the previous one beside its outcome, its board matches the native list and fleet tools exactly, and the sidebar shows open orders and strikes per unit.

### Phase 7: the control plane

**Changes.**
- **Heartbeat.** A scheduled commander wake owned by Core, firing every minute with the next unit to review and the standing rules broken now. It replaces `rotate.py`'s loop and stops when the commander thread stops.
- **Standing rules.** A `StandingRule` family; each member derives its current violations on read (an order without a deadline, an overdue order, a unit over its storage limit, an unjudged queue older than its bound, and the others `rotate.py` checks today).
- **Rule review.** The commander's rules enter its turn context as instruction segments with provenance, present on every turn, which removes the need for a review log. If a review step is kept, it is a typed record on the commander thread.
- **Alerts carry only decisions.** The commander's wake receives a reply addressed to it, a deadline crossing, a merge, a failed check and a works-in-app close, each once. A thread's messages to itself never reach another thread's feed; overdue is a state entered once and then shown on the page; disk pressure is raised when a threshold is crossed; unjudged counts are a column, not events. Measured before: about 700 of 1,136 feed lines were self-posts and OVERDUE repeated 150-170 times per order (`commander-game.md`).
- **Load and forking.** `CommanderLoad` derived per commander from look debt (units with new activity and no current prediction, and the oldest age), judge debt (the unjudged queue and its oldest age), decision inflow, prediction calibration and context used, plus a one-word self-report (`sharp`, `stretched`, `skimming`) recorded each heartbeat and compared with the measures. Over a limit, the commander forks through the existing fork operation for the oldest debt and new orders are refused until it is back under; forks are ordinary registered threads that report on their own thread names.
- **Where the time goes.** `UnitTime` (row 32) derived from Core's turn records: per unit and fleet, minutes in model time, tool runs by declared kind (build, check, test, other shell), waiting on a named owner, waiting on the commander, and idle. Each wait over a bound asks the commander one question on its card: does this need to take this long, or is it self-inflicted (a rerun, a handoff, a broken merge, a wrong order)? The answer is recorded on the wait, and a daily rollup ranks self-inflicted minutes by cause and by who caused them, the commander included. 2026-10-18 example: about 75 of 110 minutes of the install were self-inflicted (a merge never run, a converter proven on a partial copy, a reversed order, a needless handoff, two commanders).
- **Growth alarm.** A growth check derived on read from git for each configured repository: any merge to `main` that grows the source root by more than the limit (40 net lines today) appears as a fleet signal, and a verdict accepting such a delivery must state why the growth is necessary.
- **Storage hygiene.** `UnitStorage` derived on read from each unit's worktree paths and the free disk: over the worktree limit, under the cleanup threshold (30 GB today), or under the floor (5 GB today), at which operations that allocate are refused.
- **Send guard.** Stays a PreToolUse hook, since it belongs at the harness boundary; it checks the commander's sends against Core's order tools instead of the scripts.

**Deletes:** `rotate.py`, `capacity.py` and `forks/`, `growth_watch.py` and `growth_seen.json`, `storage.py`, `rules.py` and `rules.jsonl`; `send_guard.py` is rewritten against Core.

**Done when** a commander runs a full session with no script process running, every signal the scripts raised appears from Core, and replaying one recorded hour of the live feed on a copy delivers to the commander only decision events, with no duplicates, and the same hour yields a `UnitTime` rollup whose classes add up to each unit's elapsed time.

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

The phases and what each waits for are declared once, in `src/agent_comms/plan_phases.py` (the `PlanPhase` family). An order names the phase it serves with a `Serves: <phase>` line, stored as `ProblemOrder.serves`; a phase is done when every order serving it is closed. Each rotation prints `PLAN IDLE` for every phase whose dependencies are done and that has no open order (`PlanIdle` in `src/agent_comms/standing_rules.py`).
