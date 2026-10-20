---
name: claude-usage-budget
description: Claude subagent usage is the scarce budget; keep background forks to the critical path until Pi (gpt-6.1-sol) threads work on the bus
type: feedback
---

2026-10-19: I had 7 background forks running at once: runtime, carry-order, strings, index-lock, hooks, fast-ratchet and fast-board. Owner: "don't launch too many background agents, I'm trying to move to Pi Codex threads to save on usage, so please be mindful. If it's going to take another 5 hours to get Pi GPT working in comms we can't just blow through usage."

**Why:** the move to Pi threads exists to save Claude usage. Spending Claude forks freely while that move is blocked defeats it.

**How to apply:**
- Only work on the path to working Pi threads gets a Claude fork; aim for one or two at a time.
- Everything else (perf, cleanup, slop hunts) becomes a written order queued for Pi units, not a fork.
- Briefs say "finish and report in N lines", with no exploration beyond the task.
- This tempers [[slow-is-a-defect]] and [[structural-fixes-over-pushing-through]]: dispatch still happens, but to Pi units once they work. See [[codex-units-need-dictation]].
