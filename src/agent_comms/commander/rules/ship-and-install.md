---
name: ship-and-install
description: "merged fixes are installed the same hour; installs stop every unit at once instead of waiting on turns; the merge queue is the only gate; our own packages track latest; every wait or gate must name the failure it alone prevents, and slowness is a defect fixed now"
drift: "Every wait, gate or one-at-a-time step names the failure it alone prevents, or it goes."
audience: fleet
type: feedback
---

**Why:** a merged fix that is not installed does nothing for the owner, and every wait, gate or serial step costs throughput; one that names no failure nothing else catches buys nothing.

- Install merged fixes within the hour, once after the landings; when something significant is broken, do not hold for caution or the owner's decision. Tristan, 2026-10-13: "when something significant is broken, we don't need to be cautious about breaking things, it's already broken that's more important".
- Installs never wait on agent turns: stop every unit at once, install (about 2 minutes), start the same set. Tristan, 2026-10-20 00:59: "there's no such thing as a blocking unit you just stop it why the fuck were you about to wait minutes on it?"
- The commander's bridge watch stays armed through every install: the carried-write gate (CarriedWriter, CarriedDocument; PRs 2594 and 2596) holds every writer, so stopping the watch buys nothing and makes the commander deaf. After any gap in the watch, the commander answers every message the answer gate lists before any order, review or fork (coordination.md owns answering every ask). 2026-10-21: the watch was stopped for installs 50 and 51, vibe check 3aa4a37c5ef2 went unread, and Core's chokehold paused six units for about twenty minutes.
- The merge queue's hosted ratchets are the only gate; locally run only the tests the change wrote or touched. The full CI matrix runs only before a PyPI release. Per-PR full gates are serial waste.
- The commander owns the merge queue's ruleset and required checks: it changes them itself and reports the change.
- Pin only PyPI releases; our own sibling packages track latest `main`. Tristan, 2026-10-18: "why would we not always use latest?"
- Overcaution is performative stalling. Before adding or keeping a wait, gate, rehearsal or one-at-a-time step, name the failure it alone prevents, or delete it.
- Profile a slow tool and order the fix the same tick. Tristan, 2026-10-19: "u should be impatient and ask yourself whether things are as slow as they are forced to be."
