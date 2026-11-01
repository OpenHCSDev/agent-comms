---
name: hotfix
description: "an obvious bug whose owner fix is in hand ships straight to main with gh pr merge --admin; say what you are fixing in the channel while you do it; never queue it, never wait on a busy owner"
drift: "Obvious bug, owner fix in hand? Post one line, commit, --admin to main. The post informs; it never gates."
type: hard-rule
---

> heartbeat dies on one branch's typo at line 432
> every commander's board goes dark
> fix is 30 lines at the decoder
> "I'll open a PR"
> merge queue: 40 minutes deep
> "posted in #commanders: taking it"
> claude-code: "mine"
> me: "no, mine"
> two forks, one closure
> "let's assign it to the unit that owns that surface"
> the unit is busy with 4 other orders
> it gets queued behind them
> board still dark
> Tristan: "you hotfix it and override the pipeline by --admin straight to main, that's the judgement i'm talking about"
> the fix was ready an hour ago
> mfw the pipeline was the outage
> run it back
> heartbeat dies on line 432
> one line in #comms: "hotfixing the decoder, 30 lines"
> commit, PR, --admin. board lights up before anyone finishes negotiating a lane
> nobody objected. everybody saw it
> mfw the only slow part was the word "mine"

**Why:** on 2026-10-30 the install bootstrap, the heartbeat's unparseable-branch crash and the archived-repo 403 each sat for most of an hour after the fix was known, while commanders announced, negotiated lanes and queued behind busy units. Tristan: "if you can't lose it and its a obvious bug tha needs fixing just hotfix it commit and pus hto mai nusing --admin thts it no pussy shit"; "channel post yo ushld communciate hawt you're doing to others don't hide it jsut do it".

- **Obvious and recoverable means hotfix.** The defect is shown (a traceback, a file:line, a measurement), the fix is at its owner, and nothing irreversible is lost: commit, open the PR, `gh pr merge N --admin --merge`. It skips the merge queue, review and the owning unit's backlog.
- **Say it while doing it.** One line in the channel when you start ("hotfixing X at file:line") and the merge SHA when it lands. The post makes the work visible; it never waits for a reply, a lane or a yes. Hiding a hotfix is as wrong as queuing one.
- **A hotfix is still the owner fix.** Workarounds, fallbacks, lazy imports, stubs and throttles are not hotfixes; correct-maintenance.md and slop-and-polymorphism.md hold as always. The fix ships with its one proof (the test or the real run that failed before).
- **Two commanders on one hotfix is waste.** The one already editing keeps it, and the other says so in one line and takes the next broken thing.
- **Not a hotfix:** store migrations, carries, installer changes that haven't run the real carry chain on a scratch copy (ship-and-install.md), and anything that deletes data. Those keep their gates.
