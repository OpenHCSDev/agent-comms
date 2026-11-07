# R* — the ownership trace for P1's first batch

P0 requires the relation before the moves: for each declaration, who implements it and who
consumes it. This file is the map P1's first batch reads; a row with no traced owner is
`OPEN` with the reason, never a guess.

Measured at main `5161c723a` (`plans/MODULE-OWNERSHIP-PLAN.md:186` carries the census).

## The batch

| module | lines | top-level classes | family roots | importers (src) | distinct symbols consumed |
|---|---|---|---|---|---|
| `src/agent_comms/todos.py` | 7134 | 254 | 22 | 39 (31 at package root) | 95 |
| `src/agent_comms/tools.py` | 1795 | 116 | 19 | 12 (7 at package root) | 7 |
| `src/agent_comms/standing_rules.py` | 1555 | 51 | 31 | 13 (7 in `team/`) | 22 |

The import counts are the whole argument for splitting them where they are, and the symbol
counts are the whole argument against doing it by file size: `tools.py` exports **7** names to
the rest of the package while holding 116 classes, and `todos.py` exports **95**.

## `todos.py` — five consumers, five owners

Declaration anchors, then every consumer that names them:

| declaration | at | consumers (file: symbols) | owner it implies |
|---|---|---|---|
| `Todo`, `TodoStore` | `todos.py:5760`, `todos.py:6507` | `team/standing.py:7`, `turn_digests.py:6`, `ui_model/teammate_state.py:5`, `behaviour_patterns.py:4`, `owner_requests.py:3`, `plan_phases.py`, `plan_reading.py`, `held_orders.py` | the row family: stays with the store |
| `TodoCommand` and its members (`ClaimOrder`, `CorrectCheckTodo`, …) | `todos.py:4009`, `5537` | `team/order.py:31`, `team/judge.py:28`, `order_tools.py:26`, `goal_management.py:7`, `goal_actions.py:7`, `team/standing.py` | `planning/` — every consumer is an order verb |
| `OrderCompletion` / `CheckedDelivery` / `DeliveryEvidence` / `BranchPointCheck` / `DeliveryMerge` | `todos.py:208`, `267`, `284`, `383`, `560` | `team/order.py`, `order_tools.py`, `integration.py` | `execution/` — evidence about a delivered commit, no order vocabulary |
| `PullRequest`, `PullRequests` | `todos.py:703` | `integration.py:7`, `closed_pull_requests.py:7`, `team_hud_records.py:3`, `heartbeat_frame.py` | `transport/` — the GitHub boundary |
| `HeadReviewRecord`, `Assignment`, `PlainText` | `todos.py:4869`, `3786`, `2043` | `team/judge.py:28`, `team/standing.py:7`, `heartbeat_frame.py:8` | `planning/` |
| `ProgressRead` | `todos.py:1647` | `heartbeat_frame.py:8`, `turn_digests.py:6` | `planning/` (imports `transport/`) |
| `EditWorkplaces` | `todos.py:6911` | `turn_digests.py:6`, `heartbeat_frame.py:8` | left for P2 (`projects/`) |

Two rows were `OPEN` when this trace was first written. P0 does not let an open row become the
next phase's guess, so they were resolved by reading the declarations, not by consumer counts:

- `HeadReviewRecord` (`todos.py:4869`) holds `request`, `actor`, `at`, `verdict: HeadVerdict`,
  `order: HeadOrderLink`. Its fields are the verdict vocabulary, so `planning/` owns it;
  `heartbeat_frame.py` reads it as a snapshot, and a consumer that reads a record does not own it.
- `ProgressRead` (`todos.py:1647`) is *"one read of order progress"* composing `PullRequests`
  (transport facts) with `CheckoutReads` (checkout facts), keyed by `Todo`. It declares no
  fields of its own, so the order row family owns it and it imports `transport/`.
- `EditWorkplaces` (`todos.py:6911`) is `StorageRule` + registry threads + path allocations +
  held orders: the **edit boundary**, not the order vocabulary. P1 leaves it in `todos.py` and
  P2 moves it to `projects/`; moving it twice would put the storage boundary in `planning/`.

## `tools.py` — a transport module wearing a declaration module's size

Seven consumed symbols: `CommsSendTool`, `CommsInboxTool`, `CommsChannelsTool`,
`CommsSetGoalTool`, `SendTool`, `ToolReplyResult`, `invoke_tool`.

- `ToolReplyResult` and `invoke_tool` are the request/response pair every caller uses. Owner:
  `transport/tools/` (its only importers are `transport/tools` itself, `plan_phases.py` and
  `cli_verbs/`).
- The `ServedComms*Tool` family from `tools.py:466` onward (`ServedCommsChannelTool`,
  `ServedCommsHistoricalThreadsTool`, `ServedCommsExportWireTool`, …) has **no consumer outside
  `tools.py`**. Owner: `transport/tools/`, and the split must not invent one.

That second fact is the trap in this batch: 116 classes and 19 family roots look like a domain
module, and the trace says the package sees seven names. P1 splits it by those seven.

## `standing_rules.py` — one owner, already in the right package

13 importers, 7 of them in `team/`. The rule families — `StandingRule` (`standing_rules.py:185`),
`UndatedStandingRule` (`396`), `HeldBranchRule` (`538`), `OwedAnswer` (`750`),
`HeadReviewRule` (`918`), `ClearingVerb` (`138`), `Degradation` (`92`) — are consumed by
`team/` and by `plan_phases.py`, `heartbeat_frame.py`, `team_heartbeat.py`,
`behaviour_patterns.py`.

Owner: `team/`. This file's split is relocation into the package that already consumes it, and
per rules/slop.md a relocation is not factoring — so P1 moves it and claims nothing more, or
leaves it until its own census delta says otherwise.

## What P1 takes from this map

1. `todos.py` splits along the four consumer clusters above, not along line ranges: `planning/`
   (the command family), `execution/` (delivery evidence), `transport/` (the pull-request
   boundary), leaving the row/store family where it is.
2. `tools.py` splits on its seven exported names, and the `ServedComms*Tool` family travels with
   them because nothing outside needs it separately.
3. `standing_rules.py` is a move to `team/`, described as a move.
4. Both `OPEN` rows are resolved above from the declarations' own fields, so P1 starts with no
   guessed owners; `EditWorkplaces` is the one row deliberately deferred, to P2.
