---
name: coordination
description: "one order per owner holding every problem on its surface; fixes go to bus units, test and mechanical work to forks; check open PRs first; never block on a question; mind Claude usage; record new owner targets at once"
audience: commander
type: hard-rule
---

**Why:** problems scattered across orders lose their owner; on 2026-10-19, 33 of the 34 checks the Core commander switch depended on sat with no owner for nearly two hours.

- Group problems by surface into one order per owner; no problem is ever without an owner.
- Fixes, features and investigations go to bus units (launch one when all are busy): they carry an order, a review timer, strikes and a public record. Test and mechanical work goes to a fork when the commander's context makes it faster (Tristan, 2026-10-20).
- Subagents are forks, not fresh agents, and get no turn limit. Tristan, 2026-10-18: "always fork. stop it, fork and let the fork take over". Claude usage is the scarce budget: keep background forks to the critical path (2026-10-19: "don't launch too many background agents").
- Before each order, compare every open PR's files (`gh pr list --state open`) with the order's; merge the overlapping ready PR first.
- A multi-phase plan gets a tracking issue and a Core todo per phase when dispatched; every hunt first checks whether a plan phase is ready.
- Never block the loop on a question to the owner: ask in plain text with a stated default and keep working.
- When the owner states a new target, direction or rule, update the goal record the same turn. Tristan, 2026-10-09: "reflexively update your own goal whenever I say something important".
