# The commander system: remove its slop

Written 2026-10-21 by claude-ai for Tristan, from the Oct 20-21 postmortem. Every failure that night and morning was one of two shapes: one fact with several owners that disagree, or a rule written in prose that no code enforces. A prose rule nothing enforces is itself a fact restated outside its owner: the code that enforces it is the owner, and the prose is the copy. So every phase below gives one fact one owner and deletes the copies, and every rule either names its mechanism or goes.

Tristan's role in this plan is none. The phases are declared in the Plan family (#2462), so PlanIdle starts each ready phase and a phase is done only when its evidence holds on `main` and in the installed build. claude-ai's ledger checks verify each phase against that evidence.

## Phases

Each phase lists the fact, its current copies, its one owner, what gets deleted, and the evidence that it is done.

### 1. Liveness (first)

- **Fact:** whether the commander's own machinery is running.
- **Copies:** a heartbeat that only spoke on change, so silence meant both "calm" and "dead"; failures logged only to the journal.
- **Owner:** the heartbeat beats every pass; a failed service pass is a bus fact (`ServicePassFailing`); `HeartbeatSilent` is raised in the commander's own tool process; an unreadable unit checkout is that unit's own state and the pass continues for everyone else. (#2472)
- **Deletes:** the `if found == previous: return` gate in `commander_heartbeat.py`.
- **Done when:** installed, and a heartbeat arrives every pass.

### 2. Review gates merging

- **Fact:** whether a change was reviewed.
- **Copies:** `judge` over raw tool edits (`commander/judge.py`, `Edit.unjudged`) and `ReviewedHeadVerdict` on a PR head (`todos.py:2581`), plus `Unjudged` printing a count past 30 (`commander/standing.py:370`).
- **Owner:** one verdict per PR head commit, posting `agent-comms/review`; `agent-comms/review` is a required check in ruleset 25439636; `Unjudged` means ready PRs whose head has no verdict; `order send/say` refuse once one has waited 10 minutes. (P433, #2469)
- **Deletes:** the raw-edit count as an alarm, and its limit.
- **Done when:** the ruleset lists `agent-comms/review` and an unreviewed PR cannot merge.

### 3. One board

- **Fact:** what the commander should act on next.
- **Copies:** the heartbeat, the bridge watch Monitor, tool-watch, a hand-written `gh pr list` poll, `order board`, and unit DMs read as they arrive.
- **Owner:** the heartbeat, built from phase 4's unit states and the standing rules, worst first. `SKILL.md` names it the commander's input.
- **Deletes:** "Arm two Monitors" at `SKILL.md:17` and the hand-written PR poll; PR state is read by Core.
- **Done when:** `SKILL.md` has no Monitor setup and Core owns PR state.

### 4. One state per unit

- **Fact:** whether a unit is working, waiting, blocked or idle.
- **Copies:** `goal_execution.state`, runtime activity, the Todo holder, and checkout reads (`Worktree.edits`, branch commits). On 2026-10-21 Core showed fork-absence "blocked" while its runtime was thinking, and the commander called fork-directives stuck two minutes after it wrote `watched.py`.
- **Owner:** a `UnitState` family derived from those sources, urgency carried by the MRO, an unreadable source as its own member (never "idle"). Plan `commander-native-plan.md` phase 6.
- **Deletes:** every private read of the same fact in `capacity`, the standing rules and the order tool.
- **Done when:** `capacity`, the heartbeat and `order` read only `UnitState`.

### 5. Every alarm declares what clears it

- **Fact:** what action ends an alarm.
- **Copies:** alarms that no action clears: INSTALL DUE on a 30-minute clock (`todos.py:1062-1106`) said "due" 64 seconds after a finished install; `Unjudged` over raw edits grew faster than anyone could read; PLAN IDLE `record_plane` asked for a task nobody could supply in a tick. Alarms that can't be cleared teach the reader to skip the whole heartbeat.
- **Owner:** `StandingRule` declares, abstractly, the action that clears it; a rule with none cannot be declared. INSTALL DUE derives from the installed build (P431, #2475).
- **Deletes:** the INSTALL DUE schedule, the raw-edit alarm (phase 2), and phases with no declared task.
- **Done when:** every `StandingRule` member implements its clearing action.

### 6. Owner targets live in Core

- **Fact:** what Tristan has directed.
- **Copies:** one goal field (`goals.py:499`) that named one plan, while his other plans lived only in chat.
- **Owner:** `OwnerRequest` rows, any number open (P421, #2473), and the Plan family with phase evidence on `main` (#2462).
- **Done when:** every plan in `plans/` is declared and PlanIdle fires on any of them.

### 7. One install run

- **Fact:** what an install did and how it ended.
- **Copies:** a report, a string, a print and several `ValueError`s; a practice copy that walked the directory instead of asking owners; two runtimes of different builds on one root.
- **Owner:** one run result posted for every exit, practice copy included (P404, #2443); the practice copy built only from declared owners (#2478); one runtime per root, recorded by build (P420, #2455).
- **Deletes:** the directory walk, the per-schema file names from #2425.
- **Done when:** a failed practice copy posts its result, and a second build cannot attach to a live root.

### 8. Forks and assignment

- **Fact:** who is doing what.
- **Copies:** commander forks invisible to Core (`capacity` read 0 live forks at load 240); several forks picking the same idle unit (pi-cmd-tools got three orders in minutes).
- **Owner:** forks registered as threads; one assignment lock (`commander/capacity.py:14`).
- **Done when:** `capacity` counts forks and two picks of one unit cannot both succeed.

### 9. Orders

- **Fact:** which order is live, and when an order's check is accepted.
- **Copies:** hand-typed `--replaces` ids (`commander/order.py:861` restating `todos.py:3085`); check acceptance spread across callers (`goals.py:264`); order checks run in the live checkout, so a fix can't be ordered until the code it fixes is installed.
- **Owner:** the order tool resolves the live order (P438); one acceptance rule on `OrderCheck`; checks run against the order's own revision.
- **Done when:** `--replaces` is gone and an order can be checked before install.

### 10. Rules name their mechanism

- **Fact:** what the commander must do.
- **Copies:** prose in `rules/`, `voice.md`, the indictment and `SKILL.md`, with no link to the code that enforces it. Re-injection kept the prose in context all day; the commander followed what it could satisfy by writing text (voice) and skipped what needed attention (the heartbeat).
- **Owner:** each rule bullet names the standing rule, refusal or check that enforces it; `rules review` flags a bullet with none; a bullet with none is either given a mechanism or deleted. Credit goes to verified outcomes only (#2476).
- **Done when:** `rules review` reports zero bullets without a mechanism.

### 11. Transition scaffolding

- **Copies:** `commander_parity/`, `tools/carry_rts_orders.py`, the scratch copies of the old scripts.
- **Owner:** the native commander, after `commander-native-plan.md` phase 8 (cutover) passes its parity gate.
- **Done when:** those paths are gone from `main`.

## Order

Phases 1 to 3 first, because they restore what the commander can see and make review unavoidable. Then 4 and 5, which make the board honest. Phases 6 to 11 run in parallel where their files are disjoint. Every PR states its net `src` lines; this plan deletes copies, so most phases should be net negative.
