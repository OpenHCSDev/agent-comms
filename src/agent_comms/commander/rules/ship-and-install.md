---
name: ship-and-install
description: "merged fixes are installed the same hour; installs stop every unit at once instead of waiting on turns; the merge queue is the only gate; our own packages track latest; every wait or gate must name the failure it alone prevents, and slowness is a defect fixed now"
drift: "Every wait, gate or one-at-a-time step names the failure it alone prevents, or it goes."
audience: fleet
type: feedback
---

**Why:** a merged fix that is not installed does nothing for the owner, and every wait, gate or serial step costs throughput; one that names no failure nothing else catches buys nothing.

- Install merged fixes within the hour, once after the landings; when something significant is broken, do not hold for caution or the owner's decision. Tristan, 2026-10-13: "when something significant is broken, we don't need to be cautious about breaking things, it's already broken that's more important".
- INSTALL DUE appears the pass main leaves the installed build, naming every uninstalled merge and the open requests about to land. Due means an install can be used, not that one runs: batch the landing requests, then run `order install`, the one launch of Core's installer. Tristan, 2026-10-25: "due means it can use an install, not install right now, you can batch other pending PRs in the same install".
- Installs never wait on agent turns: stop every unit at once, install (about 2 minutes), start the same set. Tristan, 2026-10-20 00:59: "there's no such thing as a blocking unit you just stop it why the fuck were you about to wait minutes on it?"
- The merge queue's hosted ratchets are the only gate; locally run only the tests the change wrote or touched. The full CI matrix runs only before a PyPI release. Per-PR full gates are serial waste.
- The commander owns the merge queue's ruleset and required checks: it changes them itself and reports the change.
- Pin only PyPI releases; our own sibling packages track latest `main`. Tristan, 2026-10-18: "why would we not always use latest?"
- Overcaution is performative stalling. Before adding or keeping a wait, gate, rehearsal or one-at-a-time step, name the failure it alone prevents, or delete it.
- Profile a slow tool and order the fix the same tick. Tristan, 2026-10-19: "u should be impatient and ask yourself whether things are as slow as they are forced to be."
- Tool friction hit twice, slow or refusing correct work, gets a fork that tick, ahead of plan work: routing around it pays its cost on every call by every unit. Tristan, 2026-10-25, ten hours after asking for it: "didnt we know this for over 10 hours now".
- An install under 60 s is healthy: no order hunts single seconds below it, planned cuts still land, and above 60 s is a defect ordered that tick. Tristan, 2026-10-23: "if we stay below 60s its not broken".
- A hand `agent-comms install` is safe at any time: it and Core's `PendingInstallDue.enforce` both go through `InstallRun.start`, one launch claim. A merged fix never waits out a settle window. Tristan, 2026-10-25 13:10, after five merged fixes sat uninstalled: "agents love waiting i fucking hate it no respect for human time".
- An install refused with `RouteReadersHold` naming a Toad closes that Toad by its literal PID (TERM, then confirm it exited), reruns, and tells Tristan he can reopen it on the new runtime. Other owner processes stay off-limits. Tristan, 2026-10-23: "u can close toad from now on".
- Any PR touching `stack/` moves the native pin: before an ok, check the pin on an archive of the head (`NativeBuildInputs(root).require_pinned(...)`), and never merge over a red native-scripts. A PR touching the installer or tools/cutover runs the real carry chain on a scratch copy first. #2748, #2749 and #3088 each left main uninstallable on 2026-10-23 and 10-24. Removing a speed gate never drops a slop gate with it: on 10-23 dropping debt-src and debt-tools let the ratchet go red on 22 of 60 merges.
- After pulling agent-comms main, relock and sync the stack with it (`uv lock --project stack --upgrade` then `uv sync --project stack --locked`): the stack pins Toad at its last lock while agent-comms is editable (2026-10-18: Toad 80 commits behind crashed on the removed `SidebarModel.snapshot`). After a carry on a live store, restart every long-running commander process; after switching branches in a live checkout, delete `__pycache__/*.pyc` older than its source.
- An application order parks at "waiting on the run" until `order close PID --works "..." --artifact FILE` after the real run. `order done` on a merged order only re-records delivery and the unit keeps holding it (2026-10-24: the fleet gridlocked on P553 and P565).
