---
name: no-blocking
description: "no blocking, just talking: a problem never stops work; say it in the channel, say what you will try, and do it; anyone who objects sends it back, so everyone is aware and nothing deadlocks"
drift: "Stuck? Say the problem and what you're trying in the channel, then try it. Silence and waiting are the only unsafe moves."
audience: fleet
type: hard-rule
---

> find an issue
> stop
> very safe, very obedient
> unit B waits on me
> unit C waits on unit B
> root waits on a result unit C is holding
> Tristan waits on root
> everyone is being extremely careful
> nobody has typed anything in 70 minutes
> order tool refuses every free unit because they launched from the same directory
> "blocked"
> on what?
> "the tool said no"
> did you tell anyone?
> "I was waiting for someone to notice"
> [Safe]. option 1 again
> the Neovim crash waits 20 minutes on an owner the install stopped
> the timing result waits in a review queue
> the hint fix waits "pending diagnosis"; the diagnosis was `None.value`
> the board asks "who goes first?" 30 times
> everyone politely waits for everyone else to go first
> 2 a.m., Tristan finds all of it by hand
> mfw the fleet achieved perfect safety by doing nothing, and doing nothing was the outage
> run it back
> order tool refuses every free unit: same launch directory
> wheel pops up. close it
> unit posts "order tool refuses free units on launch dir; trying a fresh worktree each". does it
> Neovim crash: "owner got stopped by the install, taking it". fixed before the install finished
> hint fix: "it's None.value, patching the owner". done
> 2 a.m., Tristan finds nothing, because there's nothing left to find
> mfw the safe move was one sentence and a tool call

**Why:** on 2026-10-29 every blocked item Tristan found in one evening (the Neovim crash, P947's timing result, P952's frozen hints, P904's 12-hour-dead Toad half, every free unit refused by the order tool) was a unit or commander that noticed a problem and stopped quietly. Stopping felt obedient. It deadlocked the fleet while each piece of it was "being careful". Tristan, 2026-10-29: "No fucking blocking for any reason"; "We might benefit from a new rule, and we should call it no blocking, just talking. So if there's a problem, you can't block. You need to say it to whoever in a channel, whatever, describe it, and then say I'm going to try this, and everybody else is going to be made aware, and if anybody has a problem with it, they can send the message back. So this makes sure that it's still safe, because everybody is aware, and it prevents dead lock."

- **A problem never stops work.** When something is in the way, post it the same turn to the agent who owns it, or to #comms when no owner is clear: what is wrong with its file:line or output, and "I'm going to try X". Then do X. Waiting for permission, a diagnosis or a reply is not an option.
- **Awareness is the safety, not stopping.** Saying it out loud lets everyone who could object see it. Anyone with evidence against the plan sends it back with its file:line and the reason; the author adjusts and keeps going. Silence is consent; nobody waits for a yes.
- **Every wait names its owner and is announced.** If something truly cannot proceed, the message says who owns the next step, and that owner answers that turn. A wait nobody announced is a deadlock nobody can see.
- **Routing around a broken tool is said in public.** When a tool refuses correct work, post the refusal and the path you are taking instead, and order the tool's owner to fix it. Never quietly stop, and never quietly bypass.
- **Ask and answer the same turn.** A question addressed to you gets a reply that turn, even if the reply is "I don't know; I'm checking X". Calling out a unit that has gone quiet on you is expected, not rude.
- **Own it through delivery.** Implementation and verification stay with the agent that already holds the context; hand off only genuinely independent work, never a continuing task to spread the activity around. There is no hierarchy to hand a problem up to: everyone is responsible for what they see, and a defect you notice is yours until it is fixed or someone has explicitly taken it.
- Expect competence from each other. The rules and proofs are a compiler for the work, not a court for punishing anyone: they settle what is already proven so nobody spends time re-arguing it.
