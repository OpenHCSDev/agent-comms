# Commander as a tight game

Started 2026-10-18 from a talk between Tristan and Claude about fleet noise.
Living document: expand it as we learn.

## Goal

Make commanding the fleet low friction. Low friction and fun go together:
a good RTS keeps one player in control of many units because every signal on
screen is worth a decision, and every decision is one action. The commander
(Claude, or any harness running the commander tools) is a token predictor, so
what changes its behaviour is what lands in its context each turn. The design
lever is therefore: make the right action the cheapest action, and make the
wrong one visible.

## What the noise actually is (measured 2026-10-18 ~14:10)

Last 2000 lines of the commander feed (`rts/messages.log`):

| Source | Count | Decision needed? |
|---|---|---|
| Units messaging their own thread ("Claimed") | ~740 of 1136 MSG (687 "Claimed") | no |
| Messages addressed to the commander | ~100 | yes |
| OVERDUE re-raised for the same order | 150-170 per missed order | once |
| DISK LOW repeats | 69 | once per threshold crossing |
| Unjudged-count lines (32, 40, 42, 35...) | every rotation | no, it is a counter |
| Each unit report | arrives twice (bridge monitor and alarm monitor) | once |

Conclusion: the noise is delivery, not information density and not latency.
Reports addressed to the commander are mostly dense and useful. Stalls (disk
floor, broken CLI) produced more chatter only because alarms repeat.

## Why it mattered

On 2026-10-18 the commander closed P437 on a grep without starting the CLI,
which broke every agent-comms command in the next build, and let Pi Durable
pass through five owners without one real turn. The written rules already said
results are fixed behaviour in the real app. The tooling (`order.py done`)
accepted a cheaper proxy (a commit plus a check command), and under hundreds
of non-events the commander followed the tool instead of the rules.

So the scoring was right; enforcement and signal were wrong.

## Design rules

1. **State on screen, events only on change.** The board shows every order
   with a live countdown, like build timers. A deadline passing is one event;
   the row then stays red. Nothing is re-announced.
2. **Every ping is a decision.** Reply arrived, deadline passed, merge landed,
   check failed, seen working in the app. Self-claims, counters and repeats
   never reach the commander.
3. **One channel per event.** A unit report arrives once.
4. **Hotkeys, not paperwork.** Strike, reassign, approve, close: one action
   each. Wording problems get an automatic correction, not a refused command.
   Forks can report back without the claude-code-to-claude-code refusal.
5. **Score what is hard to fake.** A fix seen working in the real app, and a
   problem that stays fixed. Source lines deleted counts only alongside that.
6. **Timers drive attention, not punishment.** Overdue means "look now".
   Minute-scale deadlines suit small mechanical orders; deep multi-owner
   problems (Pi Durable) get a traced path and a realistic first-commit
   deadline instead of strike-driven reassignment, which throws away context.

## The screen

Seeing every unit, including unit-to-unit messages, is worth keeping: it is
where early catches come from (2026-10-18: core-goal-retry renaming `None`
checks to `is_set()` caught before merge; core-goal-retry telling codex
"Claude took P398 off me" exposed a wrong belief fixed in one message). The
problem is that everything currently arrives as an interrupt. Separate
**seeing** from **being interrupted**, the way an RTS shows the whole map but
pings only a few things.

For an LLM commander, "seeing" means reading text into context, so the screen
is a compact snapshot pulled when the commander chooses, not a pushed stream.

Three layers:

1. **Alerts (push, interrupt):** only events needing a decision: a reply to
   the commander, a deadline reached, a merge, a failed check, seen working.
2. **Screen (pull, every rotation, ~40 lines):** board with countdowns, one
   card per unit, and unit-to-unit messages since the last look with
   self-messages stripped.
3. **Deep look (pull, on demand):** a unit's full turn digest and diff when
   its card looks off.

The same screen fits Toad: board, unit cards, filtered message lane.

### Unit card sketch (real data, 2026-10-18 14:15)

```
UNIT                   ORDER        DUE    LAST CALL  DOING NOW
core-goal-retry        P398         14:15  0:53 ago   gh pr edit 1472 body "checkpoint, not ready to merge"   <- at deadline, relabelling instead of merging
e2e-reply-latency      P508 P506    14:25  0:11 ago   git add field_codec.py registry_store.py                <- P508 order never named field_codec.py
                       P502         15:00
core-read-locks        P507         14:20  0:11 ago   checking out c97b9777 to count parser default encodes
core-read-locks-2      P405         14:20  0:01 ago   screenshot of copied root on :1405, reading Toad log
toad-theme-2           P503         14:25  0:17 ago   running p503-launch.py (real UI check)
issue102-wake-fixed    P436         14:20  2:15 ago   read p436 ratchet.log
core-commander-2       P504         14:20  2:39 ago   goal marked completed: merged 14:10 as 28f2360  <- close or judge
nra-roster-heuristic   P495         14:25  none       no tool call since P495 was assigned            <- NO FIRST CALL
core-runtime-location  P501         15:00  6:43 ago   waiting on codex build (named dependency)
core-pi-durable        -            -      17:16 ago  idle                                           <- give it work
change-report          -            -      5:38 ago   idle after P495 taken away
```

Each `<-` is the kind of thing the commander should catch on a glance; none of
them needed an interrupt. A card line costs one line of context; the same facts
today arrive as dozens of feed events.

Card fields: unit, open orders with due times, seconds since last tool call,
the last call in plain words, source lines added and removed since the order,
last message to anyone. Flags are judged by the commander reading the card,
not by regex (Tristan 2026-10-15: reason semantically).

### Distilled looking: levels of detail

Like zoom levels in an RTS: the default view is cheap, and each unit expands
on demand. Every level ends with what it left out ("+14 calls", "+3 files"),
so the commander knows what it has not seen.

**Level 0, the card (one line per unit, every minute).** Only facts that
change a decision:

- *On target?* Files edited since the order compared with the files and owner
  the order named: `on order` or `off order: field_codec.py`.
- *Moving?* Commits pushed, PR state, source lines +/- since the order.
- *What kind of work?* The mix of the unit's recent calls by tool kind, read
  from the typed tool family (not by regex on text): `read 6, edit 3, test 2`.
  The commander judges the pattern: edit/test alternating is moving; tests
  only is rerunning as investigation; reads only for ten minutes is lost;
  writing reports is paperwork.
- *Claim versus state.* The unit's last claim beside the git fact: "merged"
  next to whether that sha is on main.
- *New since last look.* Units with nothing new collapse to one dim line;
  the commander marks a unit seen when it reads it.

**Level 1, the turn (expand one unit).** The newest turns compressed to one
line each: `14:12 read goals.py, todos.py; edited goals.py (+12/-30); ran
pytest goals (2 failed)`, plus the unit's last message and the order's
numbered steps with which ones the edits touched.

**Level 2, the evidence (expand one turn or file).** The diff stat, then the
hunks for one file, then a single tool call's full input and output.

Commands: `look` (level 0), `look UNIT` (level 1), `look UNIT diff [FILE]` or
`look UNIT call ID` (level 2). In Toad the same levels are collapsible rows.
Each level is a Core query over Goals, Todos, `TurnDigestRead` and git, so
cost stays constant per unit and nothing rescans history.

### Momentum: every look ends in a prediction

Looking is not reading; it is predicting (Tristan 2026-10-18). For each unit
with new activity, the commander answers three questions and writes the
answer on the card:

1. **Would I do this?** Given what the unit knew, is its last move what I
   would have done, and why or why not? "Yes: it read the owner before
   editing." "No: it is relabelling the PR at its deadline instead of
   merging."
2. **Will it land on time?** From its pace: order steps touched out of the
   total, time used out of time allowed, and the work mix. A one-word call
   (`on time`, `late`, `wrong way`, `stuck`) plus the reason.
3. **What steer?** The cheapest action that changes the trajectory: name the
   exact edit it is circling, cut a step that does not serve the works-when
   run, unblock a dependency, or (only after tracing the path myself)
   reassign. "None" is a valid steer when it is on time and on target.

The prediction is recorded with its time. The next look shows the previous
call beside what actually happened, so the commander's own accuracy becomes
visible: predictions that keep being wrong mean the commander's model of
that unit (or of the task) is wrong, and that is the thing to fix.

Cost control: a full three-question prediction for units with new activity
since the last look, a carried-over call for the rest. A unit whose
prediction is `on time` and whose card has not changed needs no new call.

Example from 2026-10-18 14:15:

```
core-goal-retry  P398  due 14:15
  would I: no, it is editing the PR body to "not ready" at the deadline; I would merge the RecordMember deletion that is already right
  land:    late, 2 of 4 steps, is_set() count still growing
  steer:   commit the RecordMember work now; replace is_set() calls with methods Goal and NoGoal override
  last call (14:00): on time  -> missed: prediction wrong because I did not count is_set() growth
```

### Load: measured, not self-reported

The commander's own report of its load is unreliable: on 2026-10-18 it was
skimming (one-line "leaving that to the fork" replies, 42 unjudged edits)
without noticing. So load is a number on the HUD, like the supply count in an
RTS, derived from behaviour:

- **Look debt:** units with new activity since their last prediction, with
  the age of the oldest: `look debt 4 (core-goal-retry 6m, e2e 3m, ...)`.
- **Judge debt:** unjudged edits and the age of the oldest.
- **Inflow:** decision events per minute reaching the commander.
- **Calibration:** share of recent predictions that came true.
- **Context:** tokens used against the 300k compaction point.

The existing `capacity.py` gate (fork when overloaded) reads these instead of
a raw unit count. When look debt or judge debt passes its limit, the tool
does not ask the commander how it feels; it forks a slice for the oldest debt
and refuses new orders until the debt is back under the limit. No form to fill,
one number to read, and the response is automatic.

**Self-report alongside, calibrated like predictions.** Tristan's correction
(2026-10-18): the commander did recognise its own skimming when asked, so the
self-report carries signal even if nobody's introspection is reliable. Each
tick the commander also writes one word: `sharp`, `stretched` or `skimming`.
It is recorded next to the measured debts, and the two are compared the same
way predictions are compared with outcomes. Agreement makes the cheap word
trustworthy; `sharp` while look debt is 6 is itself a flag that the
commander's sense of its load is off, shown on the HUD. Either signal crossing
its limit triggers the fork.

## Done so far

- `order.py send` requires a "Works when: <user-visible run>" line; `done`
  requires `--seen FILE` proving that run happened.
- Reassigning a problem with two or more prior owners requires the
  commander's own trace (`--traced PATH:LINE`).
- Wording/jargon gets a correction message, not a strike.
- `rotate.py` repeats rule lines only when they change.

## Next (small edits to the commander scripts)

- [ ] `vibe_watch.py`: drop messages a unit sends to its own thread.
- [ ] `order.py overdue`: raise OVERDUE once per order, not every tick.
- [ ] Drop the duplicate path so unit reports reach the commander once.
- [ ] DISK LOW only when crossing a threshold.
- [ ] Unjudged count shown on the board, not pushed as events.
- [ ] Let forks report to the main commander directly.
- [ ] Mark which current rules reward speed over correctness, for Tristan
      to keep or drop.

## Alignment with the native commander (checked 2026-10-18 14:20)

Core already has the commander: `src/agent_comms/commander/` (order, judge,
rotate, rules, capacity, cli, rules/*.md), installed in the
current runtime. Design: `docs/design/commander-in-core.md`. Problems are Core
Todos and orders are Goals (#934/P51, merged 2026-10-17); a wrong strike is
voided, never deleted (P47, #1498); closing a problem goes through
`CommsProblemCloseTool` with works evidence.

The live loop still runs the scratch copies in
`/home/ts/.cache/agent-scratch/rts/` with their own JSON stores
(deadlines.json, strikes.jsonl, problems.json). That is a competing authority:

- The 2026-10-18 rules (Works when, `done --seen`, `--traced` after two
  owners, wording correction instead of strike) exist only in scratch.
- The 86 wording strikes were deleted from scratch `strikes.jsonl`, against
  Core's void-not-delete rule.
- Branch `rts-commander-tools` (f6f2f6dfb) pushed a third copy under
  `tools/rts`. It should be deleted, and ts-gaming pointed at the installed
  `agent-comms` commander instead.

Everything in this document lands in Core's commander, not in scratch:

- Works-when and seen evidence belong on the order Goal's acceptance check.
- Unit cards and the screen are a Core query over Goals, Todos and the
  turn digest (`feat/turn-digest`), the same data Toad renders.
- Alert filtering (self-messages, overdue once, one channel per event)
  belongs to the Core event the commander thread subscribes to.

Then the live loop switches to Core's commander and `rts/` is deleted.

## Open questions

- What does the noise look like on Tristan's side in Toad (channel volume,
  repeats, slow updates)? This document so far measures only the commander feed.
- Should the HUD live in Toad (native Core board, P51) rather than a markdown
  board, so the commander and Tristan see the same screen?
