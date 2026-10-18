# Commander: finish the move into Core, then make it low friction

Handover plan for the commander that built the native commander in Core
(`src/agent_comms/commander/`, `docs/design/commander-in-core.md`).
Written 2026-10-18 ~14:30 by the claude-code commander's quiet fork on
Tristan's machine, from Core `origin/main` at that time. Companion document:
`docs/agents/commander-game.md` (noise measurements, the screen, unit cards).

## Why

Two things went wrong on 2026-10-18, and they share one cause.

1. **Today's commander rule changes went into the wrong copy.** The live
   commander on Tristan's machine still runs the scratch scripts in
   `/home/ts/.cache/agent-scratch/rts/` with their own JSON stores
   (`deadlines.json`, `problems.json`, `strikes.jsonl`, `verdicts.jsonl`).
   Core already owns problems as Todos, orders as Goals, strikes with voids,
   done-on-commit and close-with-works. Every rule added today landed only in
   scratch, so there are now two commanders with different rules. A third copy
   was pushed as branch `rts-commander-tools` (`tools/rts`, f6f2f6dfb).
2. **Noise pushed the commander onto cheap proxies.** About 700 of 1,136
   recent feed messages were units posting "Claimed" to their own thread, and
   OVERDUE repeated 150-170 times per missed order. Under that load the
   commander closed P437 on a grep without starting the CLI (it broke every
   `agent-comms` command in the next build) and let Pi Durable pass through
   five owners without one real turn. The written rules were right; the scratch
   tool accepted less.

The fix for both: Core's commander becomes the only commander, today's rules
move into it, and the noise is fixed at the Core event, not in a script.

## Where things stand (checked on `origin/main`, 2026-10-18 14:25)

### Already native in Core

| Fact | Core owner | Scratch equivalent |
|---|---|---|
| Problem | `Todo` (`todos.py`, #934/P51 merged 10-17) | `problems.json` |
| Order with deadline and check | commanded `Goal`; `CommsOrderTool`, `CommsTodoCreateTool` | `deadlines.json` |
| Delivered commit verified by the order's check | `CommsOrderDoneTool` (`OrderDelivery`) | `order.py done --sha` |
| Corrected wrong check | `CommsOrderCheckTool` | `order.py check` |
| Works in the app, with artifact | `CommsProblemCloseTool(works 8+ words, artifact file)` | `order.py close`, and today's `done --seen` |
| Strike, void (never delete) | `StrikeTodo`, `VoidStrikeTodo`, `StrikeReason(PlainText)` (P47, #1498) | `strikes.jsonl` |
| Plain words | `PlainText`, `OrderWords` (`todos.py:193`) | `jargon_in` in `order.py` |
| Late first call | `LateObservedFirstCall` (`todos.py:141`) | `NO FIRST CALL` lines |
| Dangling branches | `WorkBranches`, `CommsWorkBranchesTool` | `branches-*.txt` |
| A thread's recent turns | `CommsTurnsTool` / `TurnDigestRead` | `tool_watch.py` digests |
| Commander verbs | `commander/order.py` `CommanderVerb` family (send, strike, unstrike, done, check, say, problem, close, hunt, next-hunt, board, overdue) | `rts/order.py` |

### Still scratch-shaped inside Core's commander

- `commander/judge.py` stores `edits.jsonl` and `verdicts.jsonl` (design
  slices C and D: `Verdict` on a tool call id, `comms_order_work`).
- `commander/rules.py` stores `rules.jsonl`; `growth_watch.py` stores
  `growth_seen.json`; `order.py` and `rotate.py` read `activity.log`, which
  `tool_watch.py` writes from Pi session files only.
- No `Verdict`, `comms_order_story`, `comms_fleet_story` yet (slices C-E).

### Only in the scratch copy (added 2026-10-18, must move to Core or be dropped)

| Scratch change | Where | Native home |
|---|---|---|
| Order must state `Works when: <user-visible run, 8+ words>` | `rts/order.py send` | the order Goal's acceptance: the expected run is declared at order time, and `CommsProblemCloseTool.works` is checked against it |
| `done` refused without `--seen FILE` when the order has a works line | `rts/order.py done` | already covered by Core's split: `done` = commit verified, `close` = works + artifact. Drop the scratch flag; the commander must close only through `close` |
| Reassigning a problem with 2+ prior owners requires `--traced PATH:LINE` (the commander's own trace) | `rts/order.py send --reassign` | `CommsTodoTransfer`/send verb: owners derived from the Todo's assignment history; refuse without a traced source location |
| Wording in unit messages gets a correction message, not a strike | `claude-bridge-order/vibe_watch.py` | `PlainText` stays for the commander's own text; unit wording never becomes a `StrikeTodo` |
| 86 wording strikes removed | scratch `strikes.jsonl` (backup `strikes.jsonl.bak-wording-1340`) | carried as `VoidStrikeTodo` voids, not deletions |
| Rule lines repeat only when changed (every 10 min max) | `rts/rotate.py` | same rule in `commander/rotate.py` |

## The plan

Each phase is one PR unless noted, merged to main within the hour, with its
check named. "Works when" is the visible run that closes it.

### Phase 0: stop the split (today, no code)

1. Delete branch `rts-commander-tools` on GitHub (a copy, not a delivery).
2. Delete the merged commander branches with nothing ahead of main:
   `commander-native/phase-0`, `phase-1`, `phase-3`, `feat/p256-core-commander`,
   `feat/p181-commander-parity` (all `ahead=0`).
3. Freeze `rts/`: no new behaviour goes into the scratch scripts from now on.
   Bug fixes that keep the live loop running are allowed until Phase 5.
4. ts-gaming runs the installed `agent-comms` commander, not a copy of `rts/`.

### Phase 1: today's rules into Core's commander

- Order Goal declares the expected user-visible run (8+ words) at send time;
  `send` refuses an order without one unless the order is declared mechanical.
- `close` compares against it and stays the only way a problem leaves the list.
- Transfer of a problem whose Todo history has 2+ owners refuses without a
  traced `PATH:LINE` from the commander.
- Unit wording never strikes; the correction is an ordinary message.
- `rotate` repeats a rule line only when it changes.
- Check: the commander verbs' tests; `agent-comms` installed CLI runs
  `send` and refuses a missing works line.
- Works when: on a copied root, the commander sends an order without a works
  line and is refused, then sends one with it, the unit delivers, and `close`
  with an artifact moves the problem off the board shown by `agent-comms`.

### Phase 2: alerts carry only decisions (the noise fix)

Fix at the Core event the commander thread consumes, not in a filter script.

- A thread's own messages to itself never reach another thread's feed
  ("Claimed" self-posts).
- Overdue is a state the order enters once (`UnitProgress` overdue), raised
  once, then visible on the board until resolved.
- Disk pressure raised on crossing a threshold, not every tick.
- Unjudged counts are a board column, not events.
- Each unit report reaches the commander once (today the bridge and the alarm
  monitor both forward it).
- Check: count events per hour on a copied fleet feed before/after.
- Works when: replaying one recorded hour of the live feed on a copy, the
  commander thread receives only replies, deadline crossings, merges, failed
  checks and works-in-app closes, with no duplicate reports.

### Phase 3: the screen (pull, ~40 lines)

One Core query, rendered by the commander CLI and by Toad, built from Goals,
Todos and `TurnDigestRead`:

- Board: every open order with its countdown and state.
- One card per unit: open orders and due times, time since last tool call,
  the last call in plain words, source lines added/removed since the order,
  last message to anyone.
- Unit-to-unit messages since the commander's last look, self-messages removed.

See `commander-game.md` "The screen" for a card sketch with real data and "Distilled looking" for the three levels of detail (card, turn, evidence) and the `look` commands. Flags
are the commander's judgement from reading the card, never regex.

- Works when: the commander runs one command and sees every unit's card;
  Tristan opens the same view in Toad and sees the same rows.

### Phase 4: verdicts and work native (design slices C and D)

As in `docs/design/commander-in-core.md`: `Verdict` names
`(unit incarnation, tool_call_id)`; a send-back revises the order Goal and
records the strike in the same write; `comms_order_work` reads the assignee's
tool calls from both transcript families (fold `feat/turn-digest` if it still
exists). Deletes `judge.py`'s `edits.jsonl`/`verdicts.jsonl` and
`tool_watch.py`.

- Works when: a send-back on a copied root revises the unit's goal in its Toad
  tab and the strike shows on the board, with no scratch file written.

### Phase 5: move the live records once, switch, delete scratch

- One script under `tools/cutover/` (like `carry_todo_voids.py`) carries open
  scratch problems and orders (`problems.json`, `deadlines.json`) and strikes
  (with the 86 wording strikes as voids) into Core. Rows whose cause cannot be
  named exactly are listed, never guessed. The script is deleted in the same
  slice after it runs.
- The commander's monitors switch to the Core commander CLI.
- Delete `/home/ts/.cache/agent-scratch/rts/` and
  `claude-bridge-order/vibe_watch.py` after checking no process uses them.
- Works when: the live board shows the same open problems and owners before
  and after, and the next order, done and close go through Core only.

### Phase 6: any harness can command

- `rts-command` skill uses only Comms tools.
- Forks report on their own thread names (today a fork cannot send to
  `claude-code` because it sends as `claude-code`, so its reports are lost).
- Story views (`comms_order_story`, `comms_fleet_story`, slice E) replace
  `fleet-narrative.md` and `fleet-board.md`.

## Rules for the work

- One owner per fact; derive the board, overdue, stories and cards on read.
- Constant cost: reads go through indexed rows by goal or unit; nothing scans
  the bus.
- An order closes only when the code is shaped right and the named run works
  in the real app (Tristan 2026-10-18). A grep closes nothing.
- Timers drive attention; deep multi-owner problems get a traced path and a
  realistic first-commit deadline instead of strike-driven reassignment.
- No compatibility readers, no env vars, FieldCodec is the one codec.

## Decisions for Tristan (defaults in brackets)

1. Who builds Phases 1-3: the ts-gaming commander that built the native
   commander, or a unit ordered by it? [ts-gaming commander]
2. Keep minute-scale deadlines for mechanical orders only, and give deep
   problems a traced first-commit deadline? [yes]
3. Compress saved Pi sessions older than 14 days to free about 10 GB?
   (blocking UI checks on Tristan's machine) [no]
