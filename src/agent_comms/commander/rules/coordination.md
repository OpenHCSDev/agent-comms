---
name: coordination
description: "one order per owner holding every problem on its surface; fixes go to bus units, test and mechanical work may go to forks; check other fleets' open PRs first; never block on a question; mind Claude usage; record new owner targets at once"
type: hard-rule
---

**Why:** problems scattered across orders lose their owner; on 2026-10-19, 33 of the 34 checks the Core commander switch depended on sat with no owner for nearly two hours.

- Group problems by surface into one order per owner; no problem is ever without an owner.
- Fixes, features and investigations go to bus units as orders (launch a unit when all are busy), because they carry an order, deadline, strike and public record. Test and mechanical work goes to a fork when the commander's context makes it faster; the fork reports its PR and an `Assumed:` list. Tristan, 2026-10-20: use Claude forks for test and mechanical work.
- Before each order and tick, compare every open PR's files (`gh pr list --state open`) with the order's; merge the overlapping ready PR first.
- A multi-phase plan gets a tracking issue and a Core todo per phase when dispatched; every hunt first checks whether a plan phase is ready.
- Never block the loop on a question to the owner: ask in plain text with a stated default and keep working.
- Subagents are forks, not fresh agents. Tristan, 2026-10-18: "always fork. stop it, fork and let the fork take over", "unless you already schooled it on correct maintenance and the paper", and "don't use that [turn limit] on ur subagents".
- Claude usage is the scarce budget: keep background forks to the critical path. Tristan, 2026-10-19: "don't launch too many background agents, I'm trying to move to Pi Codex threads to save on usage, so please be mindful."
- When the owner states a new target, direction or rule, update the goal record the same turn. Tristan, 2026-10-09: "update it as you go", "use the goal malleably", "reflexively update your own goal whenever I say something important".
