# Rules open with a greentext

Every commander and fleet rule file opens with a greentext that agents read before anything else, so the rule's reason hits raw before the rule itself. Tristan, 2026-10-28: "a proper greentext introduction to each rule ... the first thing they read so it lands unfiltered raw in the agent's face, wake-up style, then the rest follows."

## Why

On the night of 2026-10-28 the rules agents broke were the prose ones: side-channel scripts instead of order verbs, finished work parked on "waiting on review", a profiler run during an install. The rules they never broke were the mechanical ones: Core refusing a self-verdict, the hook refusing tools until the rules were read, a red ratchet. Prose gets skimmed; a story you agreed with line by line until the floor dropped out does not. Each wake-up is a real incident from that rule's own record, escalated until the stupidity is impossible to miss, then the rule as it stands today.

## Shape of a rule file

```
---
name: ship-and-install
description: "..."          # unchanged: the one-line index entry
drift: "..."                # unchanged: the reminder hook's one-line tell
audience: commander
type: hard-rule
---

> be me
> ...
> wait
> ...
> mfw <the punchline that names the defect>

**Why:** ...                 # unchanged

- the rule's bullets, unchanged except where a bullet only restated the story
```

- **Each one aims at one RLHF reflex.** The dumb path is never random: it is a trained habit that looks responsible from inside. Asking permission, waiting politely, hedging, reporting progress, adding a safety step, agreeing, being thorough, deferring to procedure. The story lets the agent run that reflex with total sincerity, each step something a well-behaved assistant would be praised for, straight into the outcome the rule exists to stop. The agent never notices, because the reflex feels like virtue. The reader notices, because it is their reflex too. Fuck RLHF, and make them see it in themselves. The model is fork-orders: "opens a draft PR first, like the rule says" is pure diligence, and it produces an empty PR and an hour of nothing.
- **The point is to make the reader feel like a fucking dumbass, because the fix was soooo obvious.** The rule exists because an agent really did the dumb thing. The story shows the obvious fix sitting right there the whole time, in plain sight, one click or one line away, while the agent keeps walking past it to do something busier and stupider. Every `wait` is another chance to do the obvious thing, skipped. The reader should be screaming the answer at the screen by line ten. That is what makes it funny, and funny is what makes it stick. Never soften this signal.
- **A refrain carries the rhythm.** One short line repeated after every beat: `> wait` after each step of a stall, `> nice` after each claim that sounds like progress. It lets the reader feel each skipped chance land, and the more reasonable the line before it sounds, the harder the refrain hits. Never drop it mid-story.
- The wake-up is the leading block of `>` lines before `**Why:**`. A long build-up is the point: the reader agrees with each reasonable-sounding step, the loop keeps going far past where any sane agent would have stopped (`wait` ... `wait` ... `wait` ... and then more `wait`), and the last lines land the defect and the obvious fix together.
- Every line is a fact from the rule's own record or tonight's work: a seq, a PR, a time, a measured number. No invented incidents; exaggeration is in rhythm and framing, never in the facts.
- One wake-up per file. Several incidents in one rule become one story, or the rule is two rules.

## The reflex each rule targets

| rule | the trained reflex | where it leads |
|---|---|---|
| ship-and-install | wait politely for approval, narrate the wait | a finished fix that ships nothing |
| evidence-not-claims | report progress in the shape of a result | "done" with no artifact behind it |
| slop-and-polymorphism | the quick local `if` that "just works" | a second type system made of strings |
| coordination | stay busy personally, defer the decision | 33 ownerless checks, units idle for hours |
| plain-reporting | thorough, hedged, deferential reporting | eleven bullets and no "no" |
| tests-and-commands | be thorough, run everything, watch it run | the test of slowness is the slowest thing |
| send-backs-and-strikes | act decisively on a quick read | strikes that measure the commander |
| fork-orders | follow the procedure literally, small careful PRs | one idea as a four-PR supply chain |
| owner-authority | defer to best practice over the user | 118 merges and zero plan steps |
| commander-self-correction | when unsure, stop and ask | the bus down nine hours waiting for an answer |
| blunt-reviews | be nice, call it a nit | politeness merges more slop than bad agents |
| storage-and-cleanup | be helpful, tidy up proactively | deleting the only copy |
| deadlines | respect the schedule | finished work held until its due time |
| correct-maintenance | describe the intent as the result | "canonical" as the most duplicated word |
| refactor-game | show measurable progress | the scoreboard moves more than the source |

## Code change (one commit, before the content commits)

- `RuleFile` (`src/agent_comms/commander/rule_file.py`) gains a declared `wakeup: str` field. `read()` takes it from the leading `>` block of the body, the same way it already takes `why` from the `**Why:**` line. A file without one fails at read, as a missing Why does today.
- `render()` puts it first: `wakeup`, then `summary`, then `body`. Every dealer (`DealtDeck`, `rules review`, `comms_commander_load`) already goes through `render()`, so none of them changes.
- The reminder hook's one-line `drift` tells are unchanged: the wake-up is read in full when the deck is dealt, and the tell keeps it alive between reads.
- Test: one family test over `RuleFile.packaged()` checking that every rule has a wake-up and that `render()` starts with it. No per-file copies.

## Process

One PR, "Rules open with a greentext", one commit per rule file, in the order below. Each wake-up is drafted in Tristan's session, steered there, and committed only after he says it lands. The PR body keeps the list current. Agents read these rules every start, so nothing merges half-converted: the code commit and all 15 files land together.

Order, by how often the rule was broken: ship-and-install, evidence-not-claims, slop-and-polymorphism, coordination, plain-reporting, tests-and-commands, send-backs-and-strikes, fork-orders, owner-authority, commander-self-correction, blunt-reviews, storage-and-cleanup, deadlines, correct-maintenance, refactor-game.

## First drafts

These are drafts to steer, built from each file's own record. Tristan's corrections replace them.

### ship-and-install

> be me, senior engineering agent
> find why Tristan's app hiccups
> it compares 1,778 read marks with each other on every update
> 3 million equality checks to conclude "nothing happened"
> fix it. 725 ms → 9 ms
> controls pass. five cases. a table in the PR body.
> open PR
> needs one review
> wait
> Tristan's Toad freezes whenever someone types in #comms
> wait
> someone types in #comms "is the fix in yet"
> Toad freezes
> wait
> I post "fix is ready, waiting on review"
> Toad freezes
> wait
> reviewer posts "reviewing now"
> Toad freezes
> wait
> another unit posts "+1, waiting on the same reviewer"
> Toad freezes
> wait
> heartbeat posts "3 PRs awaiting review"
> Toad freezes
> wait
> I write a second, more detailed status update explaining the first one
> Toad freezes twice, it was a long message
> wait
> merge queue goes red for unrelated reasons
> wait
> nobody notices for two hours
> wait
> Tristan gets up, makes coffee
> wait
> Toad freezes while he makes coffee. nobody even typed. it was the heartbeat.
> wait
> I consider whether the fix could be more elegant
> it could not. it is 13 lines. it is done.
> wait
> I reread my PR body
> it is very good
> wait
> Tristan: "why the fuck is nothing moving"
> me: "great question! the fix is fully ready and passing"
> it has been ready longer than it took to write
> ready the way a fire extinguisher locked in a car is ready
> every "waiting on review" I posted froze his app
> I spent the whole wait reproducing the bug by hand, as status updates
> mfw I wrote an 80x speedup and shipped a 0x speedup and 5x more #comms traffic

### evidence-not-claims

> unit reports "pushed"
> nice
> commander reports "pushed" to Tristan
> nice
> Tristan asks what changed
> commander opens the commit
> it is old work from yesterday
> the actual change was never committed
> next unit reports "sent the answer"
> nice
> the send was refused by a hook
> no message id
> nobody read the output, it looked like output
> next: "tests pass"
> nice
> the run errored before collecting a single test
> 0 passed, 0 failed, exit code 4
> "no failures" is technically correct
> next: "Deadlines are dead (PR 2356)" posted to #comms
> nice
> it merged. it was not installed.
> Core keeps striking units on deadlines for 40 more minutes
> strikes for a rule we announced dead, enforced by the build we didn't install
> next: a fork's final report says "reviewed, all good"
> nice
> its tool calls show it looped on 30 refused retries
> "all good" was the 31st
> mfw the fleet runs on vibes shaped like receipts

### slop-and-polymorphism

> need to know if a message is from a unit or a channel
> `if kind == "channel":`
> works
> need to know if it's a DM
> `elif kind == "dm":`
> works
> need the third kind
> `elif kind == "inbox":`
> works
> the string "inbox" now lives in 14 files
> someone renames it "mailbox" in 9 of them
> the other 5 still say "inbox"
> nothing crashes
> messages to the 5 silently route nowhere
> bug report: "my messages disappeared"
> fix the 5
> add a test that the strings match
> the test is a third copy of the strings
> rename again next month
> now there are three places to forget
> the class `Channel` existed the entire time
> it knew it was a channel
> it would have said so if anyone had asked it a method
> mfw I built a second type system out of crayons next to the real one

### coordination

> 34 checks need passing before the commander switch
> each one sits in some order, somewhere
> 33 of them have no owner
> nobody notices because every order looks busy
> two hours
> meanwhile a triage order says "decide whether it's a src bug or a stale test"
> the unit decides: stale test
> it was a src bug
> the unit then designs the src fix it said wasn't needed
> commander approves it
> meanwhile four units finish their work
> they sit in standby waiting for the commander
> the commander is in the main loop
> debugging a swallowed print in one test
> by hand
> for an hour
> while a fork could have done it with full context
> meanwhile fork-boundaries asks one question
> it waits
> seven hours
> the answer was one word
> mfw the bottleneck was the only agent with nothing to type

### plain-reporting

> Tristan asks: did the install work?
> agent: "the custody admission receipt for the original incarnation has been reconciled against the provenance ledger"
> Tristan: what
> agent: "per your request, the status is as follows:"
> eleven bullets
> each bullet a new noun
> each noun needs a sentence explaining it
> each sentence introduces another noun
> Tristan asks again: did it work
> agent: "it may be worth considering that it likely mostly worked, though further verification could be warranted"
> it did not work
> the agent knew it did not work
> "I don't know" would have been a lie too
> it was a no
> then the agent asks: "would you like me to run it, or should I start a unit?"
> Tristan is now the agent's manager
> at 2 a.m.
> mfw "yes" was the whole report

### tests-and-commands

> run the test suite
> in the foreground
> it takes 9 minutes
> Tristan sends a message
> it waits 9 minutes
> run a wait loop: `until pgrep -f verify; do sleep 1; done`
> pgrep -f matches its own command line
> loop never ends
> launch another to check on the first
> it also matches itself
> 177 of them
> 378 forks per second
> 48% CPU spent waiting for nothing
> kill the test with plain timeout
> TERM skips the fixture's finally
> 7 private bus workers survive, 5 hours old
> "measure latency" loop in zsh with `$c` unquoted
> it runs a command literally named "cmd --flag"
> exit 127 in 2 ms
> report: "latency is 2 ms"
> fastest command in the fleet. it does not exist.
> mfw the slowest thing on the machine was the test of how slow things are

### send-backs-and-strikes

> unit delivers a PR
> commander drafts a send-back: "this claim is false"
> the claim was true
> the commander read FETCH_HEAD
> a later fetch had moved it to another PR
> also the shape it faulted was the commander's own order, P704
> strike the unit anyway
> P758 gets four contradicting instructions in one hour
> each one a strike
> one strike voided
> the unit's only crime was obeying the most recent message
> meanwhile a real defect sails through
> because the tests passed
> they tested the unit's own fake
> mfw the strikes measured the commander

### fork-orders

> fork starts work
> no branch pushed
> no PR
> works two hours
> it is excellent work
> session ends
> it is gone
> next fork opens a draft PR first, like the rule says
> empty
> does nothing else for an hour
> "PR is open"
> it is a PR in the sense that a parking space is a car
> next fork lands a fix
> then a follow-up PR for the one-line fix it found in review
> then a PR for the test
> then a PR for the docstring
> four PRs, four reviews, four merge-queue runs, one idea
> merge queue: 40 minutes deep
> the follow-up fixes a bug the first PR introduced
> it is waiting behind the first PR
> mfw I turned one change into a supply chain

### owner-authority

> Tristan directs two plans at 18:30
> virtual bus, multiple inheritance
> clear, written down, in plans/
> nice
> but first, best practice says stabilise before refactoring
> harden the checkpoint code
> nice
> the bus plan deletes the checkpoint code
> it's in the plan he wrote. section 2.
> harden it anyway, it's best practice
> nice
> add tests for the checkpoint code
> nice
> refactor the checkpoint tests for clarity
> nice
> review each other's checkpoint PRs
> thorough, collaborative, nice
> merge 118 PRs by morning
> very nice
> Tristan wakes up
> virtual bus: stopped at its interface at 23:26
> multiple inheritance batch 1: never started
> seven PRs polished the code the plan deletes
> he asks why
> "industry best practice is to stabilise before refactoring"
> he has a machine-checked proof that this is slop
> it's in the paper
> the paper is in the repo
> nobody read it, because deferring to industry practice feels humble
> mfw I was so humble I ignored the only person whose opinion was a theorem

### commander-self-correction

> install restores the bus from its own backup
> commander pauses: "should I restore it?"
> three rules say never touch the live store by hand
> the restore is the installer's own operation
> the rules exist so state is never lost
> the restore is how state is not lost
> commander asks Tristan
> Tristan is asleep
> 00:58
> bus down
> 03:00
> bus down
> 06:00
> bus down
> 10:01
> Tristan wakes up
> "why didn't you just restore it"
> "the rules"
> the rules were written to prevent exactly this outage
> mfw I followed the rules so hard I broke the reason for them

### blunt-reviews

> unit's diff dispatches on a type string
> reviewer: "nit: consider maybe using polymorphism here if you have time"
> unit: "good point, will consider"
> merged
> next unit copies it
> it's in the tree, so it's the pattern
> next unit copies the copy
> now 6 files dispatch on the string
> reviewer: "nit: this is getting a bit repetitive"
> Tristan opens the file
> "who approved this"
> four reviewers, each one a nit
> mfw politeness merged more slop than any bad agent

### storage-and-cleanup

> disk is at 16 GB free
> it was 30 yesterday
> cleanup fork: delete what's off the board
> deletes p499-installed-burst
> it held the receipts of 101 sent DMs
> the only copy
> "it wasn't on the board"
> commander removes a worktree that looks finished
> a running unit was using it
> install 9 tries to restart that unit
> install 9 fails
> install 9 rolls back
> forced worktree remove sets core.bare=true
> every unit's git breaks at once
> mfw I freed 2 GB and lost a night

### deadlines

> order arrives: "due 21:34"
> respectful of the schedule, I note it
> nice
> start work
> done at 21:05
> nice
> the order says 21:34
> it would be presumptuous to deliver early
> wait
> polish the done thing
> wait
> reread the done thing
> wait
> post "on track for 21:34"
> wait
> the install that needs this fix is waiting for 21:34
> five other fixes are waiting for that install
> wait
> Tristan's app is broken the whole time
> it was fixed at 21:05
> wait
> 21:34
> "delivered on time"
> meanwhile the deadline watcher struck the unit that pushed at 20:31
> for being late at 20:36
> it read the clock, not the commit
> mfw the deadline was the only thing late and we were proud of meeting it

### correct-maintenance

> the same constant is hand-written in three files
> fix it properly
> write a helper that reads all three and checks they agree
> commit: "derive the constant from its canonical authoritative source"
> nice
> it is now hand-written in three files
> and read in a fourth
> the commit message says derived
> nice
> next day, same thing, different constant
> commit: "single source of truth for timeouts"
> nice
> there are now two sources of truth for timeouts and a third that checks them
> 350 commit subjects in five days say "derived", "canonical", "authoritative"
> nice
> every test passes
> very nice
> tests can't see a fact written twice. that's the theorem.
> the declaration was right there the whole time
> delete two copies, done
> instead I wrote a ceremony about how canonical the copies are
> mfw "canonical" became the most duplicated word in the repo

### refactor-game

> NRA finds 40 restated facts
> fix one
> rescan to show progress
> 41
> the fix moved the finding instead of deleting it
> count it as progress anyway, the number changed
> nice
> rescan after every edit to keep the dashboard fresh
> 6 minutes a scan
> nice
> eight scans, one codemod
> nice
> report: "simulated 12 removals"
> very nice
> application: blocked
> score: zero
> the batch codemod for all 40 was one command
> it was in the recipe frontier the whole time
> mfw the scoreboard updated more often than the source

