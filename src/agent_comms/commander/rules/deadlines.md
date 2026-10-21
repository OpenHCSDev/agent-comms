---
name: deadlines
description: "no deadlines on orders; the commander keeps a review timer per held order, like item timers in a CPMA match, and a timer up means read the unit's state, never an automatic strike"
drift: "A timer up means read the unit's state now; a clock never strikes and never reassigns."
audience: commander
type: feedback
---

**Why:** a due time is a target the unit hits by the cheapest route, so it ships half-finished work early, and a clock that strikes on its own strikes working units. On 2026-10-20 the deadline watcher struck P331 at 20:36:55 although PR 2351 was pushed at 20:31:45, and the stall watcher struck P358 at 20:38:20 while its unit ran the ordered tests. Tristan, 2026-10-20: "it promotes stalling we should give no deadline to the unit and the commander should keep track regularly like timers for items in a q3 cpma match".

- An order carries no due time. It names the event that starts the work ("the moment the turns are idle") and the done list.
- `order send` sets one review timer per order: the median observed seconds per edit across delivered orders times the order's edits, rounded up to a minute, two minutes minimum. The unit never sees it. When it is up, the heartbeat says `TIMER UP: UNIT PID` once per window until delivery, and the commander reads the worktree, commits, PR and newest turn that tick: at the armour spawn when it comes up, not after.
- A clock alone never strikes and never reassigns. Strikes come from a verdict on evidence (rules/send-backs-and-strikes.md); stalling is still struck.
- A unit that folds in deleting real slop on its surface is credited for the initiative. Tristan, 2026-10-20 11:53: "we allow it and reward the unit for taking that initiative".
