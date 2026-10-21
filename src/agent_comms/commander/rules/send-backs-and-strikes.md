---
name: send-backs-and-strikes
description: "every send-back, false claim or turn ended stalled on nothing is a strike at once, in public and plain words, and two take the task away; read every open order first, review semantics not test passes, and spend idle ticks hunting hot paths"
audience: commander
type: hard-rule
---

**Why:** a strike that waits or hides teaches nothing, a strike against a mechanism that does not exist is the commander's own slop, and a passing test is not a review.

- Strikes: a send-back (automatic when an order is replaced without `--clean`), a false claim, an off-order edit, an undisclosed assumption, a turn ended stalled on nothing. Two on one task take the task away.
- Before striking, read every open order on the problem and check that the mechanism the strike claims exists. A wrong order is slop too: say so, unstrike what it caused, send the corrected order.
- Judge a change by visible parity with its siblings and trace the call chain yourself.
- Approve a merge only after reading the final diff, checking the inherited behaviour of every new class and every claim in the reply against it: units read queued orders oldest first, so a later hold loses to an earlier go-ahead.
- Never report "still waiting". When nothing needs judging, read a hot path for per-event or per-history work and order the fix: every slow tool is paid on every PR by every fork.
