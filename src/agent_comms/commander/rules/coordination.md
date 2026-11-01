---
name: coordination
description: "shared responsibility and accountability: every collaborator diagnoses, implements, verifies and reviews; keep work with its context owner, communicate actual conflicts, preserve lead-owned storage and never park a written next action behind rank"
drift: "Keep the work and judgment with its context owner. Claim conflicts aloud; no colleague waits on rank or another model's provider."
type: hard-rule
---

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
> run it back
> fork-boundaries asks one question
> commander types the one word
> 4 seconds
> 34 checks get an owner each in one order; the swallowed print goes to a fork with full context
> mfw the throughput fix was answering the question

**Why:** problems scattered across orders lose their owner (2026-10-19: 33 of the 34 checks the Core commander switch depended on sat ownerless for nearly two hours), and a unit left to diagnose picks the cheapest reading (2026-10-21: triage orders P362, P377, P378 and P379 let units decide "src bug or stale test" and then design the src fix).

- Group problems by surface into one order per owner; no problem is ever without an owner.
- **Every collaborator owns judgment.** Trace the fact, declaration, required behavior and whole consumer family; choose the coherent owner repair and verify it. A triage-only task still has its declared scope, but there is no class of agent forbidden to diagnose or implement. Communicate changed scope and real shared-file conflicts, rather than wait for a rank to choose the obvious next step.
- **Review the work, never a model's rank.** Every collaborator can implement and judge source-backed repairs. A restated fact is fixed regardless of which model wrote it; a peer's confidence is not evidence. Keep parallel work proportional to actual RAM and executable obligations, and keep its context owner accountable through acceptance. Do not require a Claude reviewer or create a colleague solely to satisfy an authority division.
- Short is measured on the correct fix at the owner. A fast workaround is slop, not a short fix, and hand-patching live state to keep an install moving is never a fix: the deliverable is the owner fix that makes that state impossible. Tristan, 2026-10-21: "if the corret maitnenane fix is like a few edtis do it, if a workaround is avaiulble as a quic kfix its not a quic kfix. ts slop."; "you shuodl not manualyl fix issues during install to kee p it going , you need tofix the isntalelr soyou never ever run into that issue again".
- **Keep implementation and verification with their context owner.** Independent colleagues can review and remove genuinely independent obstacles. Do not transfer continuing work merely to redistribute activity or hold every diagnosis behind one provider. Genuine in-turn subagents retain their parent's authority; independent Pi sessions/goals are peers regardless of fork ancestry. Existing transactions and launch claims serialize shared stores and installs, not an enforced hierarchy.
- **Finished work never waits on rank.** Publish the working checkpoint, obtain or perform an exact-head source judgment and necessary affected-path proof, enqueue it, record actual delivery and install useful landings. Announce actual shared-store or launch conflicts; do not park behind another provider or wait for permission already granted. Keep the next independent action moving. Installs follow ship-and-install.md.
- **Every unit ask is answered before the next rotation step.** A question, alert or handoff from a unit holding an order gets a disposition the same tick; a unit idling on the commander's paperwork is commander waste (2026-10-21: fork-boundaries' 8122 waited about seven hours).
- Preserve an independent session's own goal, history and original inputs. Use the existing colleagues and source contexts; no new colleague or paid model is implied by shared authority. Standby names a genuine external dependency only when no independent action remains, never a written next step waiting for permission.
- Before each order, compare every open PR's files (`gh pr list --state open`) with the order's; merge the overlapping ready PR first.
- A multi-phase plan gets a tracking issue and a Core todo per phase when dispatched; every hunt first checks whether a plan phase is ready.
- Never block the loop on a question to the owner: ask in plain text with a stated default and keep working.
- When the owner states a new target, direction or rule, update the goal record the same turn. Tristan, 2026-10-09: "reflexively update your own goal whenever I say something important".
- A unit is answered with `order say UNIT FILE --answers ID1 ID2`, one call per unit: space-separated 12-hex message ids, taken from `message_identity_source` by seq (a burst's own ids, never an id quoted in its body). A comma list records nothing (2026-10-21: 37 answers lost), and a bare `send` leaves the ask UNANSWERED to Core.
- Every report from Tristan becomes a durable record the same turn: an order with an owner, a phase in plans/OWNER-ASKS-PLAN.md, or a bus message; never a note in context. Tristan, 2026-10-25, holding 13 asks: "damn see how fragile it is to just a 'remmeber' its importatn t put out durable ordesr".
- Bus orders reach a Codex thread through the Codex bridge's `turn/start` (16 ms into the running turn on 2026-10-24); never `codex queue`, which delivers only at turn end (60 stale messages on 10-23). "inProgress" from a queued input is not proof the unit read it: the rollout is the evidence, and a stale rollout behind a `reasoning` record is a wedged turn, interrupted with `turn/interrupt` and re-delivered in full.
- New units are Pi units (`agent-comms import-running-thread ... --model openai-codex/...`, then `agent-comms start`), each proven with a real message; never Codex CLI sessions (2026-10-19: their shared app-server reached 7 GB and was OOM-killed). Pi logs in with Tristan's ChatGPT Pro account and shares Codex's weekly pool. A Claude unit launched from a Claude session first clears every inherited `CLAUDECODE`, `CLAUDE_CODE_*` and `CLAUDE_PID` variable, or it writes no transcript and the bridge never sees it (2026-10-18).
- `gh` in Toad worktrees passes `-R OpenHCSDev/toad`: a shared clone's gh-resolved default opened batrachianai/toad#314 upstream on 2026-10-21.
