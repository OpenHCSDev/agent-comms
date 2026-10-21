---
name: deadlines
description: "no deadlines on orders; the commander keeps a review timer per held order, like item timers in a CPMA match, and a timer up means read the unit's state, never an automatic strike"
type: feedback
---

**Why:** a due time is a target the unit hits by the cheapest route, so it ships half-finished work early, and a clock that strikes on its own strikes working units. On 2026-10-20 the deadline watcher struck P331 at 20:36:55 although PR 2351 was pushed at 20:31:45, and the stall watcher struck P358 at 20:38:20 while its unit ran the ordered tests; both took the task from the only unit holding the work. Tristan, 2026-10-20: "it promotes stalling we should give no deadline to the unit and the commander should keep track regularly like timers for items in a q3 cpma match".

- An order carries no due time for the unit. It names the event that starts the work ("the moment the turns are idle") and the done list.
- The commander owns one review timer per held order, set from the measured median per edit. A timer up means the commander reads the unit's worktree, commits, PR and newest turn the same tick, the way a CPMA player is at the armour spawn when it comes up, not after.
- Strikes come only from a commander verdict on evidence: a send-back, a false claim, an off-order edit, or a turn ended stalled on nothing (SKILL.md "Stalled on nothing"). A clock alone never strikes and never reassigns.
- Stalling is still struck: no tool call while holding an order, a question it could answer itself, ceremony instead of the next step. The timer is when the commander looks; the evidence is what it judges.
- A unit that folds in deleting real slop on its surface is credited for the initiative. Tristan, 2026-10-20 11:53: "we allow it and reward the unit for taking that initiative".
