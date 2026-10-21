---
name: coordination
description: "one order per owner holding every problem on its surface; units gather evidence and the commander diagnoses and chooses the shape; every unit ask is answered before the next rotation step; check open PRs first; never block on a question; record new owner targets at once"
drift: "Units gather evidence; you diagnose and choose the shape. Answer a unit's ask before your next rotation step."
audience: commander
type: hard-rule
---

**Why:** problems scattered across orders lose their owner (2026-10-19: 33 of the 34 checks the Core commander switch depended on sat ownerless for nearly two hours), and a unit left to diagnose picks the cheapest reading (2026-10-21: triage orders P362, P377, P378 and P379 let units decide "src bug or stale test" and then design the src fix).

- Group problems by surface into one order per owner; no problem is ever without an owner.
- **Units gather evidence; the commander diagnoses and shapes.** A unit reproduces, traces, measures and reports with file:line. The commander decides what the defect is and the exact shape of the fix (owner, parents, members, what gets deleted). A triage order classifies and reports only; any src edit goes out as a new order whose shape the commander wrote.
- Short fix: the commander writes it, commits and pushes. Long fix: a bus unit with exact numbered edits (launch one when all are busy). Test and mechanical work goes to a fork when the commander's context makes it faster (Tristan, 2026-10-20).
- **Every unit ask is answered before the next rotation step.** A question, alert or handoff from a unit holding an order gets a disposition the same tick; a unit idling on the commander's paperwork is commander waste (2026-10-21: fork-boundaries' 8122 waited about seven hours).
- Subagents are forks, not fresh agents, and get no turn limit. Tristan, 2026-10-18: "always fork. stop it, fork and let the fork take over". Claude usage is the scarce budget: keep background forks to the critical path.
- Before each order, compare every open PR's files (`gh pr list --state open`) with the order's; merge the overlapping ready PR first.
- A multi-phase plan gets a tracking issue and a Core todo per phase when dispatched; every hunt first checks whether a plan phase is ready.
- Never block the loop on a question to the owner: ask in plain text with a stated default and keep working.
- When the owner states a new target, direction or rule, update the goal record the same turn. Tristan, 2026-10-09: "reflexively update your own goal whenever I say something important".
