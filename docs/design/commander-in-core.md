# Commander in Core: shipped owners and remaining work

Source baseline: **`origin/main` at `011084fc5`**, checked 2026-10-19. Paths and line
numbers below refer to that revision, not an installed build. This is a source
reconciliation, not evidence that the parity session or the scratch-state move ran.
The implementation order belongs to the `PlanPhase` family in
`src/agent_comms/plan_phases.py:28`; the remaining work belongs to
[the commander-native plan](../../plans/commander-native-plan.md#reconciled-gaps-phase-2-source-check).
Its gap rows G1–G18 cover the differences described here.

## Entry points, not another tool catalog

`ToolRequest.schema` derives each tool's wire name and parameters from its declaration
(`src/agent_comms/tools.py:308`), and `ToolRequest.invoke` decodes through `FieldCodec`
(`src/agent_comms/tools.py:327`). Read the actual catalog with `agent-comms tools`;
`ToolsCliCommand` uses `NativeRequestTools.catalog`, not a commander-specific list
(`src/agent_comms/tools.py:1315`).

The shipped invocation is **`agent-comms tool --from THREAD --tool NAME --arguments JSON`**,
not `agent-comms invoke`: `InvokeCliCommand` declares the CLI name `tool`
(`src/agent_comms/tools.py:1325`). Caller identity comes from that explicit argument through
`ToolCaller.from_actor` (`src/agent_comms/tool_caller.py:26`). This does not mean the
commander scripts are configuration-free: they still bind their caller from the installed
`CommanderConfig` (`src/agent_comms/commander/order.py:51`,
`src/agent_comms/commander/config.py:52`).

## Step zero: stop the split

Core still packages the commander adapter entrypoint
(`pyproject.toml:60`, `src/agent_comms/commander/cli.py:14`). The remaining adapter modules
include the minute loop, live tool watcher, judge, capacity and send hook
(`src/agent_comms/commander/rotate.py:26`,
`src/agent_comms/commander/tool_watch.py:66`,
`src/agent_comms/commander/judge.py:272`,
`src/agent_comms/commander/capacity.py:74`,
`src/agent_comms/commander/send_guard.py:155`). Their presence is not proof of a second
order store: the order adapter reads Core's Todos and invokes native tools
(`src/agent_comms/commander/order.py:78`, `src/agent_comms/commander/order.py:83`).

`StopTheSplitPhase.delivered` records the earlier branch deletion and scratch freeze
(`src/agent_comms/plan_phases.py:60`). Source alone cannot verify the state of Tristan's
scratch directory or deleted remote branches. The operational comparison remains G16;
no new behaviour belongs in a scratch copy.

## Phase 0: retained storage

`TypedStore` accepts a tuple of declared row types, derives their schema and rejects a
mismatched existing schema (`src/agent_comms/typed_table.py:791`,
`src/agent_comms/typed_table.py:809`, `src/agent_comms/typed_table.py:895`).
`TodoStore` declares **`Todo` and `HuntRecord`** in `todos.sqlite3`
(`src/agent_comms/todos.py:2139`). Strikes, voids, releases, verdicts, corrections and
order-message references are fields of the Todo row, **not separate strike/verdict tables**
(`src/agent_comms/todos.py:1958`).

The current goal model is `GoalOrder`, with `UnorderedGoalOrder`, `RequestGoalOrder` and
`ProblemGoalOrder` (`src/agent_comms/goals.py:402`, `src/agent_comms/goals.py:448`,
`src/agent_comms/goals.py:472`, `src/agent_comms/goals.py:516`). Checks are `OrderCheck`
with declared `CheckResult` members (`src/agent_comms/goals.py:231`,
`src/agent_comms/goals.py:261`). These are the shipped types; the plan's historical
reinstatement wording does not establish that the old author-type names exist.

## Phase 1: one order owner

- `comms_todo_create` creates the problem and order through `CreateProblemGoalAction`;
  `comms_order` assigns through `OrderGoalAction`. The tool replaces the supplied commander
  with its actual caller (`src/agent_comms/tools.py:838`, `src/agent_comms/tools.py:850`,
  `src/agent_comms/goal_actions.py:544`, `src/agent_comms/goal_actions.py:556`). Each order
  must name an active commander request, an existing owner location and check directory
  (`src/agent_comms/goal_actions.py:526`).
- `comms_todo` applies revision-checked `TodoOperation` records; `comms_order_check` changes
  a check without resending, and `comms_order_serve` names the declared phase
  (`src/agent_comms/tools.py:717`, `src/agent_comms/tools.py:879`,
  `src/agent_comms/tools.py:897`, `src/agent_comms/todos.py:1581`).
- `comms_todo_list` reads this commander's Todos and derives `OrderProgress` from git and
  PRs (`src/agent_comms/tools.py:798`, `src/agent_comms/todos.py:376`). Commander queries
  use the creator's thread incarnation, not a global problem list
  (`src/agent_comms/todos.py:2275`). The adapter's board is generated from those results
  but still written to a file (`src/agent_comms/commander/order.py:389`).
- `comms_order_done` verifies **one exact order ID** and records delivery; it **does not
  complete or otherwise change the unit's Goal** (`src/agent_comms/tools.py:857`). A
  `ProblemGoalOrder` groups the unit's assigned Todos and refuses direct Goal completion
  (`src/agent_comms/goals.py:516`). This is the shipped P603 behaviour, not the previous
  design's completion-on-done claim.
- `comms_problem_close` records installation through `InstallTodo`; it requires a delivered
  order, a file and the closing-run description, then closes the Todo
  (`src/agent_comms/tools.py:913`, `src/agent_comms/todos.py:1919`,
  `src/agent_comms/todos.py:1317`).
- `comms_work_branches` derives branches ahead of main with PR/issue links and filters
  unlinked branches on request (`src/agent_comms/tools.py:968`). This is a read tool,
  not proof that completion refuses every unlinked branch (G4).

`ToolCaller` has explicit actor selection, not the former environment-based caller
(`src/agent_comms/tool_caller.py:26`). The adapter's writes use `ThreadCaller(ME)`
(`src/agent_comms/commander/order.py:78`). This establishes the native order path, not
an observed day without private-state writes (G16).

## Phase 2: design reconciliation

This document describes the source baseline above. The plan records every remaining
difference below as G1–G17. `DesignReconciledPhase` already has an earlier delivery marker
and inherits `MergedPhase.done`, which returns true without checking this document
(`src/agent_comms/plan_phases.py:50`, `src/agent_comms/plan_phases.py:72`). That marker is
not evidence that the current design or later phase finish lines were verified (G17).

## Phase 3: the record plane

**Text and order shape.** `OrderWords` skips its declared quoted, backticked, indented and
path/file spans; it does not implement the old design's blanket exemption for every
hyphenated identifier (`src/agent_comms/todos.py:698`). `PlainText` rejects its banned
words but inherits a one-word minimum; **`StrikeReason`**, not `PlainText`, requires ten
words and rejects the declared quoted-jargon-only strike case
(`src/agent_comms/todos.py:746`, `src/agent_comms/todos.py:753`). `OrderText` rejects soft
wording (`src/agent_comms/todos.py:769`). `CheckedProblemOrder` validates edits, banned
items, report and times, while the script still parses and checks additional prose
requirements (`src/agent_comms/todos.py:1137`, `src/agent_comms/commander/order.py:180`).
Full native order-quality parity remains G1.

**Closing run and deletion.** `ApplicationOrderRun` requires eight words and a closing
report quoting four consecutive words of that run; `MechanicalOrderRun` requires no such
run (`src/agent_comms/todos.py:963`, `src/agent_comms/todos.py:972`). `FamilyDeletion`
supplies a zero-count check to delivery, but `NoOrderDeletion` remains allowed
(`src/agent_comms/todos.py:1024`, `src/agent_comms/todos.py:1029`,
`src/agent_comms/todos.py:84`). The existence of a scanner command does not prove it
catches renamed decisions (G1). Installation checks a file exists, not its runtime's
Core/Toad revisions or containment of the delivered commit (G5;
`src/agent_comms/todos.py:1923`).

**Assignment and strikes.** `Assignment` retains previous owners and checks a traced
location when moving to another owner after two distinct owners
(`src/agent_comms/todos.py:1466`, `src/agent_comms/todos.py:1496`). `Todo.assigned` invokes
that check only while an assignment still exists; release-then-assign preserves history
but skips that check (G18; `src/agent_comms/todos.py:2040`,
`src/agent_comms/todos.py:2047`). Delivered orders refuse
transfer (`src/agent_comms/todos.py:1302`). `StrikeTodo` records the reason; `VoidStrikeTodo`
keeps the original strike and its void (`src/agent_comms/todos.py:1622`,
`src/agent_comms/todos.py:1776`). The two-strike limit is on `ProblemGoalOrder`
(`src/agent_comms/goals.py:519`). `comms_fire` releases all open problems held by that unit
**from the calling commander** in one Todo-store transaction
(`src/agent_comms/tools.py:762`, `src/agent_comms/todos.py:2358`). Recording a second strike
does not itself call fire; typed causes/caught stages and automatic limit handling remain
G2. A commit delivered late records its strike during delivery; operation delivery does
not (`src/agent_comms/todos.py:100`, `src/agent_comms/todos.py:137`,
`src/agent_comms/todos.py:1892`).

**Verdicts and wording.** `comms_verdict` takes `OrderVerdict` members
(`src/agent_comms/tools.py:729`). `AcceptedVerdict` checks four order words; peer acceptance
checks a retained peer message, and supersession checks a replacing order
(`src/agent_comms/todos.py:1691`, `src/agent_comms/todos.py:1715`,
`src/agent_comms/todos.py:1737`). A send-back records a strike and routes the holder's Goal
revision through `SentBackOrderGoalAction` (`src/agent_comms/todos.py:1758`,
`src/agent_comms/goal_management.py:68`, `src/agent_comms/goal_actions.py:393`). The native
verdict validates a nonempty tool-call ID, not the existence/content of that original call;
the judge adapter still adds diff-quote/read-code checks (G3;
`src/agent_comms/todos.py:1681`, `src/agent_comms/commander/judge.py:162`).
`comms_correct_wording` sends a correction without a strike; published text can also be
recorded as `WordingCorrection` on held Todos (`src/agent_comms/tools.py:774`,
`src/agent_comms/todos.py:2345`).

**Delivery.** `CommitDelivery` distinguishes the checked commit from the delivered pushed
ancestor, rejects a merge as the unit's delivered answer, and requires an origin branch
containing it (`src/agent_comms/todos.py:148`). The older single-commit delivery shape is
readable but refused for a new verification (`src/agent_comms/todos.py:109`).
`OperationDelivery` runs the check on current state (`src/agent_comms/todos.py:175`).
`OrderCheck.at` reuses git-note results by commit and command, otherwise checks a detached
checkout (`src/agent_comms/goals.py:323`). `comms_order_done` also requires verified progress
claims, but `MergedClaim` checks origin containment, not specifically a merge to main
(`src/agent_comms/tools.py:867`, `src/agent_comms/progress_claims.py:44`). Distinct landing
states and a completion-time PR/issue requirement remain G4.

**Deadline visibility.** Orders still render `Deadline:` into unit instructions
(`src/agent_comms/todos.py:1221`), contrary to parity row 33's commander-only deadline (G6).

## Phase 4: the review queue

`comms_order_work` reads the current assignee's turns since its order; `OrderWork` folds
calls by `tool_call_id`, newest first, with recorded running/succeeded/failed state
(`src/agent_comms/tools.py:940`, `src/agent_comms/order_work.py:13`,
`src/agent_comms/transcript_events.py:353`, `src/agent_comms/transcript_events.py:381`).
The shared `TranscriptRead` delegates source selection to the thread's execution owner,
using native runtime history or `ExternalTranscript` (`src/agent_comms/turn_digests.py:84`).

`comms_unjudged` already exists. It returns **edit calls**, excludes IDs already judged on
each open order, and has a count-only external read for load
(`src/agent_comms/tools.py:738`, `src/agent_comms/turn_digests.py:117`,
`src/agent_comms/turn_digests.py:140`). It is not a queue of all calls and unit messages
since a last-verdict cursor (G7). The separate native-file-following `tool_watch` loop and
judge rendering/alarm still exist (`src/agent_comms/commander/tool_watch.py:66`,
`src/agent_comms/commander/judge.py:83`, `src/agent_comms/commander/judge.py:107`).
The blanket constant-cost/no-bus-scan promise is not established: peer acceptance walks
newest wire messages, and `TurnsSince` admits every turn since the order (G14;
`src/agent_comms/todos.py:1718`, `src/agent_comms/turn_digests.py:73`).

## Phase 5: the story

Verdicts, strikes, voids and releases are already on Todos
(`src/agent_comms/todos.py:1958`). Public rendering/posting is still a separate adapter
step: `order.on_record` appends a narrative file and then posts, while `BackVerb.report`
posts its roast after the verdict (`src/agent_comms/commander/order.py:414`,
`src/agent_comms/commander/judge.py:262`). Native send-back recording only routes through
`Goals.send_back` (`src/agent_comms/goal_management.py:68`). Native order/fleet story tools
and record-owned, once-only posting remain G8, not shipped features.

## Phase 6: the commander page

Core has a frontend-neutral UI model and observation process
(`src/agent_comms/ui_model/__init__.py:1`,
`src/agent_comms/ui_model/observation.py:71`). The existing thread row contains ordinary
thread presentation/unread data, not commander board/strike fields
(`src/agent_comms/ui_model/sidebar.py:41`). The commander board still renders to the
configured Markdown file (`src/agent_comms/commander/order.py:389`). The commander model,
five panes, unit detail, predictions and time accounting remain G9 and G13. This source
pass makes no claim of Toad page acceptance without that Core model.

## Phase 7: the control plane

| Capability | What the source implements | Remaining plan row |
|---|---|---|
| Heartbeat | `rotate.main` is still a script loop printing the next unit and sleeping 60 seconds (`src/agent_comms/commander/rotate.py:26`). | G10: Core-owned scheduled wake, stopped with the commander. |
| Standing rules | `StandingRule.broken` discovers members and derives violations; `comms_standing_rules` enforces them before returning lines (`src/agent_comms/standing_rules.py:25`, `src/agent_comms/tools.py:811`). This is not a read-only tool. `FlatWheelSpin.enforce` can strike/reassign (`src/agent_comms/commander/standing.py:120`). | G12: remove installed-script/config dependencies and finish parity. |
| Rule review | `comms_rule_review` writes typed draws/answers in `RuleReviews`; `RulesDue` uses the 20-minute freshness bound (`src/agent_comms/tools.py:826`, `src/agent_comms/rule_reviews.py:131`, `src/agent_comms/standing_rules.py:146`). The script still selects rules and refuses actions when due (`src/agent_comms/commander/rules.py:57`, `src/agent_comms/commander/order.py:690`). | G12: instruction/context ownership or complete native review path, not a claim that rule files already enter every turn. |
| Load/forks | `capacity.Load` derives open/overdue counts and unjudged edits, but live forks are filesystem `.beat` files (`src/agent_comms/commander/capacity.py:24`, `src/agent_comms/commander/capacity.py:36`). `LoadedVerb` enforces overload in the adapter (`src/agent_comms/commander/order.py:762`). | G11: registered forks and native load enforcement, measured debt/calibration/context/self-report. |
| Growth | `MergeGrowth` derives git growth over a 12-hour window with a 40-line source limit; it also flags test growth (`src/agent_comms/commander/standing.py:180`). It still reads repositories from installed `CONFIG` (`src/agent_comms/commander/standing.py:226`). | G12: project configuration and delivery-verdict justification. |
| Storage | `StorageRule`, `UnitStorage`, `DiskLow` and `MemoryLow` derive quotas/pressure; allocation checks use the 5-GiB floor, cleanup uses 30 GiB (`src/agent_comms/storage_rules.py:61`, `src/agent_comms/storage_rules.py:184`, `src/agent_comms/storage_rules.py:221`, `src/agent_comms/standing_rules.py:228`, `src/agent_comms/standing_rules.py:252`). | G12/G16: script-free parity session, not another storage implementation. |
| Send guard | The Claude Bash hook gets the configured commander and uses Core's unit list; it blocks raw sends to those units (`src/agent_comms/commander/send_guard.py:86`, `src/agent_comms/commander/send_guard.py:155`, `src/agent_comms/commander/config.py:105`). | G15: project-scoped commander selection and hook cutover. |
| Decision-only alerts | Rotation passes an in-memory previous-wake timestamp; undated standing rules remain broken on every read (`src/agent_comms/commander/rotate.py:28`, `src/agent_comms/standing_rules.py:50`). | G10: durable once-only decision delivery, not timestamp filtering or repeated printed lines. |
| Time accounting | `OrderCall` carries ID/name/digest/state, and `TurnDigest` holds events (`src/agent_comms/transcript_events.py:429`). Those records are not a classified elapsed-time rollup. | G13: model/tool/wait/idle accounting and wait judgments. |

## Phase 8: cutover

Installed scripts still share one machine-wide `commander.json`; `CommanderConfig.write`
creates state/forks/orders directories (`src/agent_comms/commander/config.py:71`,
`src/agent_comms/commander/config.py:83`). `install` supplies the shared commander state
path (`src/agent_comms/cli_commands.py:1467`). Core already scopes Todo queries by commander
incarnation (`src/agent_comms/todos.py:2275`); this does not remove that machine-wide
adapter configuration. Project-scoped commander registration/configuration is G15
(the work described by PR #1587).

The remaining scratch move, exact strike-cause/void preservation, shadow comparison and
removal of script/private-file dependencies are G16. No code citation can prove those
operational acts happened; the plan's parity gate still requires their recorded run.

## Phase 9: the skill

The packaged skill still instructs `agent-comms-commander` loops, file-based board work
and a `growth-watch` command (`src/agent_comms/skills/comms-commander/SKILL.md:10`,
`src/agent_comms/skills/comms-commander/SKILL.md:19`,
`src/agent_comms/skills/comms-commander/SKILL.md:46`). The command dispatcher discovers
actual package modules (`src/agent_comms/commander/cli.py:14`); there is no
`growth_watch.py` module at this baseline. Tool-only instructions and declared rule loading
remain G12/G16, with the skill rewrite tracked as G16.

## How to recheck missing declarations

The file:line references above identify shipped behaviour and its current boundary. For
absence claims, also search the pinned tree, not a fixture or a remembered tool list:

```sh
git grep -nE 'class .*Story|CommsOrderStory|CommsFleetStory|CommanderLoad|UnitTime' \
  011084fc5 -- src/agent_comms
git grep -nEi 'commander|prediction|strike' \
  011084fc5 -- src/agent_comms/ui_model
git ls-tree -r --name-only 011084fc5 src/agent_comms/commander
```

These searches locate declarations/consumers or show their absence; they are not behavioural
proof. Source reconciliation does not replace the plan's installed run and real-fleet parity
gate. Reads that still walk a feed or all order-era turns remain work, not exceptions to a
claim that all reads are indexed and constant-cost (G14).
