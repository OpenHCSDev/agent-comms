# Commander in Core: orders and their story, native to Comms

Design for moving the commander's order book and its public record out of the scratch scripts
(`/home/ts/.cache/agent-scratch/rts/{order,judge,tool_watch}.py`) into Comms Core, so any
harness (Toad/Pi, Codex, OpenCode, Claude Code) runs a commander thread through Core's declared
tools, and every unit can read the record about it.

This replaces the untracked draft `docs/agents/native-narrative-design.md` (2026-10-16). The
corrections below come from reading `origin/main` at 7c08f472b, PR #934 (`feat/commanded-goals`,
head f969806a6, the same commit as `feat/commanded-todos-integration`) and the three scripts.

## Status and new slices (2026-10-18)

Added by the claude-code commander on Tristan's machine. Slices S0-S3 come before the remaining slices C-G below; the reasons and design behind S2 and S3 (noise measurements, levels of detail, momentum predictions, measured load) are in `docs/agents/commander-game.md`.

### Why this update

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

### Where things stand (`origin/main`, 2026-10-18 14:25)

#### Already native in Core

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

#### Still scratch-shaped inside Core's commander

- `commander/judge.py` stores `edits.jsonl` and `verdicts.jsonl` (design
  slices C and D: `Verdict` on a tool call id, `comms_order_work`).
- `commander/rules.py` stores `rules.jsonl`; `growth_watch.py` stores
  `growth_seen.json`; `order.py` and `rotate.py` read `activity.log`, which
  `tool_watch.py` writes from Pi session files only.
- No `Verdict`, `comms_order_story`, `comms_fleet_story` yet (slices C-E).

#### Only in the scratch copy (added 2026-10-18, must move to Core or be dropped)

| Scratch change | Where | Native home |
|---|---|---|
| Order must state `Works when: <user-visible run, 8+ words>` | `rts/order.py send` | the order Goal's acceptance: the expected run is declared at order time, and `CommsProblemCloseTool.works` is checked against it |
| `done` refused without `--seen FILE` when the order has a works line | `rts/order.py done` | already covered by Core's split: `done` = commit verified, `close` = works + artifact. Drop the scratch flag; the commander must close only through `close` |
| Reassigning a problem with 2+ prior owners requires `--traced PATH:LINE` (the commander's own trace) | `rts/order.py send --reassign` | `CommsTodoTransfer`/send verb: owners derived from the Todo's assignment history; refuse without a traced source location |
| Wording in unit messages gets a correction message, not a strike | `claude-bridge-order/vibe_watch.py` | `PlainText` stays for the commander's own text; unit wording never becomes a `StrikeTodo` |
| 86 wording strikes removed | scratch `strikes.jsonl` (backup `strikes.jsonl.bak-wording-1340`) | carried as `VoidStrikeTodo` voids, not deletions |
| Rule lines repeat only when changed (every 10 min max) | `rts/rotate.py` | same rule in `commander/rotate.py` |

### Slice S0: stop the split (today, no code)

1. Delete branch `rts-commander-tools` on GitHub (a copy, not a delivery).
2. Delete the merged commander branches with nothing ahead of main:
   `commander-native/phase-0`, `phase-1`, `phase-3`, `feat/p256-core-commander`,
   `feat/p181-commander-parity` (all `ahead=0`).
3. Freeze `rts/`: no new behaviour goes into the scratch scripts from now on.
   Bug fixes that keep the live loop running are allowed until slice F moves the live records.
4. ts-gaming runs the installed `agent-comms` commander, not a copy of `rts/`.

### Slice S1: today's rules into Core's commander

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

### Slice S2: alerts carry only decisions (the noise fix)

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

### Slice S3: the screen (pull, ~40 lines)

One Core query, rendered by the commander CLI and by Toad, built from Goals,
Todos and `TurnDigestRead`:

- Board: every open order with its countdown and state.
- One card per unit: open orders and due times, time since last tool call,
  the last call in plain words, source lines added/removed since the order,
  last message to anyone.
- Unit-to-unit messages since the commander's last look, self-messages removed.

See `commander-game.md` "The screen" for a card sketch with real data , "Distilled looking" for the three levels of detail (card, turn, evidence) and the `look` commands, and "Momentum" for the prediction every look must end in (would I do this, will it land on time, what steer), recorded and compared with what happened, and "Load" for the measured look debt and judge debt, plus a one-word self-report calibrated against them, that gate new orders and trigger forks. Flags
are the commander's judgement from reading the card, never regex.

- Works when: the commander runs one command and sees every unit's card;
  Tristan opens the same view in Toad and sees the same rows.

## What the scripts do today

| Fact | Where it lives today | Problem |
|---|---|---|
| Problems | `problems.json`, `closed.jsonl` | Second problem list beside Core's Todo store |
| Orders: deadline, check, assignee | `deadlines.json` | Second order list beside the Goal |
| Order text and send-backs | `sent.jsonl` (copy of each sent body), the bus | Copied text; replies are not linked to the order |
| What a unit did | `edits.jsonl`, written by `tool_watch.py` from Pi session files only | Copies transcript content; guesses argument spellings; blind to CLI-bridge units |
| Verdicts on edits | `verdicts.jsonl` | Only the commander's session can read or write them |
| Strikes | `strikes.jsonl` | Same |
| Public story | `docs/agents/fleet-narrative.md`, appended by `order.py strike` | A second copy of the strikes, frozen at write time |
| Board | `docs/agents/fleet-board.md`, regenerated by `order.py board` | Derived, but from the scratch stores |
| Dangling branches | `branches-agent-comms.txt`, written by hand | Not derived, not queryable |

## Corrections to the 2026-10-16 draft

1. **#934 is not on main.** Main's `todos.py` is the older store (no `TodoWhere`,
   `AcceptanceCheck`, `CommandedGoalAuthor`, `Deadline`, `GoalReport`, `fleet.py`). All of those
   live on #934, which is still open and owned by core-goal-retry. Order work stacks on #934; it
   does not copy it. Work that does not need #934 lands on main first.
2. **Slice 0 needs no new derivation.** Core already projects every `ToolRequest` to the CLI:
   `agent-comms tools` emits the catalog and `agent-comms invoke --tool NAME --arguments JSON`
   decodes through FieldCodec once. The Pi extension already uses exactly this. What is missing
   is the caller: `invoke` reads the calling thread from `AGENT_COMMS_THREAD`, so Claude Code and
   other shell harnesses (which identify with `--from`) cannot call thread tools as themselves.
   Fix: `invoke --from THREAD` decodes the caller at the CLI boundary; `ToolCaller.from_environment`
   is deleted and the Pi extension passes `--from`. Then #934's five Todo `*CliCommand` classes
   (and the `FleetCliCommand` that `CommsFleetTool` forwards to) are deleted; `invoke` is their CLI.
   Edits for a new order operation: 1 class (was 2 plus help text).
3. **No message link (`OrderLink`, `OrderMessageIndex`) is needed.** An order's text is its Goal
   text; a send-back is a Goal revision by the same commander (`CommandedGoalAuthor.revised`,
   which #934 declares but never calls); the unit's answer is its structured `GoalProgress`
   report. All three are already recorded per transition in `GoalHistoryEntry`. That history is
   what replaces `sent.jsonl`. Free conversation is not an order fact and stays ordinary messages.
   This deletes draft slice 1 entirely.
4. **Send-back is an operation, and it is missing.** `revised()` and `revision_count` exist on
   #934 but nothing revises an order, so `SpinningUnitProgress` can never match. The send-back
   verdict is the one caller: it revises the order Goal (new text, `issued_at`, `revisions + 1`)
   and records the strike in the same write.
5. **Verdicts identify a tool call, not a transcript entry.** Both transcript families already
   carry `ToolStartTranscript.tool_call_id` (native Pi entries and CLI-bridge external rows), so a
   verdict names `(unit incarnation, tool_call_id)`. The unfinished `feat/turn-digest` branch
   (no PR, no worktree, 12:35) already reads a unit's recent turns from both families; it is
   folded into the order-work slice instead of being rewritten.
6. **The code ratchet's word list is a different fact.** `BannedVocabulary` on #1022 bans four
   words in code identifiers and comments. `PlainText` refuses sixteen words in prose written about
   a unit. Same idea, different requirement and scope; they stay separate declarations.
7. **Todo storage accepts declared tables only.** `TodoStore.__init__` refuses any table other
   than `todos`, so verdict and strike tables need that check to read a declared tuple of tables,
   not a second store file.

## Lessons from running the scripts (claude-code, 2026-10-16)

The Core version must not repeat these:

1. **A done check runs on the delivered commit, not on some checkout's HEAD.** `order.py done`
   refuses unless a checkout's HEAD equals the sha, so merged work cannot be closed once the
   unit's worktree moved on, and work merged into a non-main base (P55 via #1011 into #1007's
   branch) cannot be closed at all. #934's `AcceptanceCheck._check` has the same rule. The check
   runs in a throwaway detached worktree of the reported commit (`git worktree add --detach`,
   removed afterwards), and where the commit landed is derived, not reported:
   `MergedToMain` when `origin/main` contains it, `MergedToBranch(name)` when only another
   branch does, `NotMerged` otherwise. These are distinct delivery states.
2. **Plain words must not flag names it quotes.** "lifecycle" inside `toad-close-lifecycle` is a
   thread name, not prose. `PlainText` treats a word joined to its neighbours by `-`, `_`, `/`,
   `.` or `:` as an identifier, and skips quoted and backticked spans.
3. **Firing moves every open order at once.** At the strike limit, one operation lists all the
   unit's open orders and transfers or releases each with the same stated reason; nobody does
   it by hand order by order.
4. **Owners include agents outside Toad.** A Claude Code agent (claude-p60, claude-ui, ...)
   takes orders by registering a thread (`agent-comms register --name NAME`), which has the
   ordinary agent role, so it can hold a Goal. No second owner kind is added.
5. **One order may carry several problems.** Orders batch related problems for one owner. #934
   binds exactly one Todo to one Goal and refuses a second open Goal per unit, so a batched
   order is unrepresentable there; the order Goal needs a tuple of `TodoRef`s.

## What Core owns (extend, do not copy)

- **Problem = `Todo`** (#934 `todos.py`): text, `where` (`SourceTodoWhere` path:line or
  `MessageTodoWhere`), `acceptance` (`AcceptanceCheck`: command on the reported commit, delivery
  time from that commit), state family, exclusive `TodoAssignment`. Replaces `problems.json`;
  `todo-complete` with a passing check replaces `order.py close`.
- **Order = commanded `Goal`** with `CommandedGoalAuthor` (#934), `Deadline`, `late`,
  `delivery_time`. `todo-assign` / `todo-transfer` replace `order.py send` / `--reassign`;
  `ClearGoalAction.end_current` on transfer replaces `drop`.
- **Report = `GoalReport`** (#934 `goal_progress.py`): stupid, deleted, delivery, next.
- **Order history = `GoalHistoryEntry`** (main): every transition with before/after Goal.
- **Fleet = `UnitObservation` / `UnitProgress`** (#934 `fleet.py`): done, overdue, tool-silent,
  spinning, moving, idle. Replaces `order.py overdue`'s OVERDUE / NO FIRST CALL / IDLE lines.
- **Tool calls** = `ToolStartTranscript` events in native and external transcripts (main).

## New facts and their one owner

### Work location (HARD RULE, Tristan 2026-10-16)

A problem being worked on has its work on main, or on a branch with a linked PR or issue; never on
a branch with neither. Core derives the violations on read, never stores them:

- `WorkBranches(repo checkout)` reads `origin/*` branches ahead of `origin/main` with
  `git for-each-ref` and one `git rev-list --count` per branch, and the forge links with one
  `gh` GraphQL query per repository: open PRs by head branch, and branches linked to open issues.
- Each branch is one declared state: `LinkedBranch(pr|issue)` or `UnlinkedBranch`.
- The owner is a fact, not a guess: the registered thread whose `worktree` has that branch
  checked out (`git worktree list`). A branch nobody has checked out is `NoCheckout`, listed as
  such.
- Tool `comms_work_branches` (CLI: `invoke --tool comms_work_branches`) lists them, newest first.
- With #934: the order Goal's `GoalReport` delivery already names the commit; completion refuses
  when that commit is on an unlinked branch and not on main.

### Verdict (record on the order)

```
Verdict(goal: GoalRef, tool_call: str, judge: RecordedThreadIncarnation,
        outcome: type[VerdictOutcome], quoted: str, reason: PlainText, at: float)
```

- `AcceptedVerdict`: `quoted` must appear in that tool call's input, and `reason` must quote at
  least four words of the order text (judge.py `ok --serves`).
- `SentBackVerdict`: revises the order Goal with the send-back text and records a `SentBackStrike`
  in the same write. A send-back is a strike, with no warnings first.

### Strike (record on the order)

```
Strike(goal: GoalRef, unit: RecordedThreadIncarnation, cause: type[StrikeCause],
       what_happened: PlainText, caught: CaughtAt, at: float)
```

- Causes: `SentBack`, `MissedDeadline`, `FalseDone`, `MissingEvidence`. `caught` is the stage
  family (`review`, `install`, `live`, `tristan`) plus who caught it, as `order.py strike --caught`.
- `MissedDeadline` is recorded when an order is delivered with `Goal.late`; `FalseDone` when
  `AcceptanceCheck` refuses a reported completion.
- `StrikeLimit` (two) is declared on `CommandedGoalAuthor`: at the limit, one `comms_fire`
  operation transfers or releases every open order of that unit with one stated reason; there is
  no scattered `>= 2`.
- The commander is not exempt: a strike on the commander is the same record.

### Plain words

`PlainText` is a `str` value type for `Verdict.reason` and `Strike.what_happened`. It refuses text
under ten words and any word of its declared `banned` set, except inside quoted or backticked
spans and inside identifiers (words joined by `-`, `_`, `/`, `.` or `:`): custody,
admission, admit*, receipt*, checkpoint*, provenance, authority, witness*, lattice, surface,
disposition*, invariant*, semantic*, ownership, lifecycle, acceptance.

### The story (derived on read)

`OrderStory.for_goal(goal)` merges the order's `GoalHistoryEntry` transitions, its verdicts and its
strikes by time; each event type renders its own plain line. `FleetStory.recent(limit)` is every
strike and send-back, newest first. `fleet-narrative.md` and `fleet-board.md` become outputs of
`comms_fleet_story` and `comms_todos`, not files anyone appends to.

## Operations (one `ToolRequest` each; CLI is `invoke --from THREAD --tool NAME`)

| Tool | Who | Does | Replaces |
|---|---|---|---|
| `comms_todo_create` | anyone | existing (#934) | `order.py problem` |
| `comms_todo_assign` | creator | existing; issues the order Goal | `order.py send` |
| `comms_todo_transfer` | creator | existing | `send --reassign`, `drop` |
| `comms_todo_complete` | assignee | existing; failed check records `FalseDone` | `done`, `close` |
| `comms_todos` | anyone | existing; the board | `board` |
| `comms_fleet` | anyone | existing; overdue, silent, spinning, idle | `overdue` |
| `comms_work_branches` | anyone | new; branches ahead of main and their links | `branches-*.txt` |
| `comms_order_work` | anyone | new; assignee's tool calls during the order, newest first | `tool_watch.py`, `judge.py show` |
| `comms_verdict` | order's creator | new; send-back revises the order and strikes | `judge.py ok/back` |
| `comms_strike` | order's creator | new; misses that are not a verdict | `order.py strike` |
| `comms_order_story` | anyone | new; one order's story | — |
| `comms_fleet_story` | anyone | new; public record | `fleet-narrative.md` |

## Moving the current records (one shot, outside src)

`tools/cutover/move_rts_records.py` runs once, then is deleted in the slice that lands it: open
problems become Todos, open deadlines become assignments, strikes become Strike rows (rows whose
cause cannot be named exactly are listed for the commander, never guessed). Verdicts and
`edits.jsonl` are not moved: they judged copies, and old tool calls can be judged again from the
transcripts if needed. After it runs, the rts scripts and stores are deleted.

## Order of the PRs

| # | Slice | Base | Depends on |
|---|---|---|---|
| S0 | Stop the split: delete copies and merged branches, freeze `rts/` | — | none |
| S1 | Today's rules into Core's commander (works line, traced reassignment, wording correction) | main | none |
| S2 | Alerts carry only decisions | main | none |
| S3 | The screen: cards, levels of detail, momentum, load | main | S2 |
| A | `invoke --from`; `comms_work_branches`; this design | main | none |
| B | Delete #934's duplicate Todo/Fleet CLI classes | #934 | #934 |
| C | `PlainText`, `Verdict`, `Strike`, `StrikeLimit`, send-back revision, late and false-done strikes | #934 | B |
| D | `comms_order_work` from transcripts (folds `feat/turn-digest`) | C | C |
| E | `comms_order_story`, `comms_fleet_story` | D | D |
| F | One-shot move of the rts records; delete rts scripts | main | E installed |
| G | rts-command skill uses only Comms tools | — | F |

Before starting a slice, check whether it already landed: `CommsWorkBranchesTool` (part of A) is on main; whether `invoke --from` (A) and the B deletions landed was not checked on 2026-10-18.

## Rules this design keeps

- Every fact has one owner; the story, the board, overdue and dangling branches are computed on
  read. Messages are referenced, never copied.
- Constant cost: reads go through indexed rows keyed by goal or unit; nothing scans the bus.
- No env vars, no compatibility readers; FieldCodec is the one codec.
- An order closes only when the code is shaped right and the named run works in the real app
  (Tristan 2026-10-18). A grep closes nothing.
- Timers drive attention; deep multi-owner problems get a traced path and a realistic
  first-commit deadline instead of strike-driven reassignment.

## Decisions for Tristan (defaults in brackets)

1. Who builds Phases 1-3: the ts-gaming commander that built the native
   commander, or a unit ordered by it? [ts-gaming commander]
2. Keep minute-scale deadlines for mechanical orders only, and give deep
   problems a traced first-commit deadline? [yes]
3. Compress saved Pi sessions older than 14 days to free about 10 GB?
   (blocking UI checks on Tristan's machine) [no]
