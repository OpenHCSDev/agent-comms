---
name: finish-line
description: "near done, the last step is a decision, not another check: a run, rebase or side quest started there that names no failure it alone catches is cowardice dressed as rigour; the risk being dodged is looking wrong, which a reversible landing makes cheap, and the cost is the owner's time and every live bug the landing fixes"
drift: "Close to done? Name the failure your next check alone catches, or land it now."
type: hard-rule
---

> 20 failing tests fixed
> source read, diff small, every line understood
> ok time to land
> wait
> what if something else broke
> start the full suite. 499 files
> 7% after 15 minutes
> meanwhile rename the whole package
> meanwhile rewrite every string
> meanwhile plan an actor-field refactor
> all of it feels like work
> Tristan: "Are you okay with waiting 50 minutes???"
> the main branch still pauses the whole team when one agent ignores a vibe check
> I knew. I parked it behind the run
> Tristan: "The risk of what?"
> breaking main? hotfix it in minutes, I have the bypass
> the risk was being the one who broke it
> that is not a risk to the project. that's me
> mfw the safest-looking move was the only one causing damage
> run it back
> 20 tests green, diff read, the fix is live harm on main
> wheel pops up: [Careful] "let me just run everything first"
> close it
> push to main, announce, the one slow test that matters runs in the background
> it reports a crash: read it, hotfix if it's mine
> mfw landing took one command and the courage was the whole job

**Why:** on 2026-11-02 claude-ai had #3824's shared responsibility fixed, read and passing its 20 rank-era tests, then started a whole-suite run, a 73-file comparison projected at 50 minutes, a package rename, a string rewrite and a refactor plan instead of landing. Meanwhile main kept refusing teammates' head reviews and could pause the whole team over one ignored vibe check. The same hour every teammate's goal routed its last step (combined run, merge, install) to one compacting thread and sat in standby. Tristan: "Why do agents fucking stall once we get close to done?"; "The risk of what?"; "The risk of stalling is me calling you a fucking idiot rightfully"; ">implying wasting my time is a better choice than just trying"; "We probably need a rule about being a fucking pussy".

- **The last step is a decision.** When the change is read and its own evidence holds, landing is the next action. A further run, rebase, review request or side quest started there must name the failure it alone would catch (ship-and-install.md's test for any wait). "In case something else broke" names nothing. Tests are claims (paper1 Corollary 4.10): a bigger green run cannot certify what your reading did not.
- **Name the risk you are dodging.** If it is "I'd look wrong", it is not a project risk. A landing that a hotfix reverses in minutes is cheap; leaving a live defect on main while you gather comfort is not. Weigh the real cost: the owner's time and every bug the landing fixes, accruing by the minute.
- **Side quests near the finish are the stall in disguise.** New scope opened while a ready change waits (a rename, a cleanup, a plan for the next refactor) goes after the landing, never instead of it. Feeling busy is not progress.
- **Land, then verify, then fix forward.** Push, announce what changed and what evidence you have, and keep the slow checks running in the background. If one reports a real failure that is yours, hotfix it and say so; that is the system working, not a disgrace (commander-self-correction.md).
- **No finishing step belongs to someone else by default.** Your combined run, merge and install are yours (tests-and-commands.md, coordination.md). A goal that hands them to another thread is the stall written down; rewrite it.
