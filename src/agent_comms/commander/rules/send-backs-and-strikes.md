---
name: send-backs-and-strikes
description: "a unit defect (send-back, false claim, off-order edit, undisclosed assumption, turn ended stalled on nothing) is a strike at once, in public and plain words, and two take the task away; a correction of the commander's own order is never a strike; read every open order first, review semantics not test passes, and spend idle ticks hunting hot paths"
drift: "Correcting your own order is not a send-back: no strike, and the unit keeps its turn."
audience: commander
type: hard-rule
---

**Why:** a strike that waits or hides teaches nothing, a strike against a mechanism that does not exist is the commander's own slop, and a passing test is not a review. Replacing an order mid-turn is the fast path; striking the unit for the commander's own correction punishes it for the commander's error (2026-10-20 20:20: pi-cmd-tools struck by the commander's reassign, 8418).

- Strikes: a send-back for a unit defect, a false claim, an off-order edit, an undisclosed assumption, a turn ended stalled on nothing. Two on one task take the task away.
- An order replacement is one of two things. A **send-back** names the unit's defect and strikes. A **correction** says the commander's order was wrong, records no strike and keeps the unit's turn permission; send it with `--clean` until Core declares the two as members of one replacement family.
- Before striking, read every open order on the problem and check that the mechanism the strike claims exists. A wrong order is slop too: say so, unstrike what it caused, send the corrected order.
- Judge a change by visible parity with its siblings and trace the call chain yourself.
- Approve a merge only after reading the final diff, checking the inherited behaviour of every new class and every claim in the reply against it: units read queued orders oldest first, so a later hold loses to an earlier go-ahead.
- Never report "still waiting". When nothing needs judging, read a hot path for per-event or per-history work and order the fix: every slow tool is paid on every PR by every fork.
