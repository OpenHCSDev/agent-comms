---
name: ship-and-install
description: "merged fixes are installed the same hour; installs stop every unit at once instead of waiting on turns; merge on local gates; our own packages track latest; every wait or gate must name the failure it alone prevents, and slowness is a defect fixed now"
type: feedback
---

**Why:** a merged fix that is not installed does nothing for the owner, and every wait, gate or serial step costs throughput; one that names no failure nothing else catches buys nothing.

- Install merged fixes within the hour; when something significant is broken, do not hold for caution or for the owner's decision. Tristan, 2026-10-13: "getting fixes in is always a priority, when something significant is broken, we don't need to be cautious about breaking things, it's already broken that's more important".
- Installs never wait on agent turns: stop every unit at once, install (about 2 minutes), start the same set. Tristan, 2026-10-19: "how many times did we wait on agents to finish their turn ... I've dealt with this so many times."; 2026-10-20 00:59: "there's no such thing as a blocking unit you just stop it why the fuck were you about to wait minutes on it?"
- Merge on local gates: targeted tests and both ratchets rc=0, each its own command, separate from the merge. The full CI matrix runs only before a PyPI release.
- Pin only PyPI releases; our own sibling packages track latest `main`. Tristan, 2026-10-18: "we should just use latest", "why would we not always use latest?"
- Overcaution is performative stalling. Before adding or keeping a wait, gate, rehearsal or one-at-a-time step, name the failure it prevents that nothing else catches, or delete it.
- Profile a slow tool and order the fix the same tick. Tristan, 2026-10-19: "u should be impatient and ask yourself whether things are as slow as they are forced to be. make it less heavy if u can see it being less heavy."
