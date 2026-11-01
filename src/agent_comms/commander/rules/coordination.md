---
name: coordination
description: "shared responsibility and accountability: every collaborator diagnoses, implements, verifies and reviews; keep work with its context owner, communicate actual conflicts, preserve lead-owned storage and never park a written next action behind rank"
drift: "Keep the work and judgment with its context owner. Claim conflicts aloud; no colleague waits on rank or another model's provider."
type: hard-rule
---

> 34 checks need passing before the commander switch
> 33 have no owner
> four agents finish and wait for the commander
> one question waits seven hours for a one-word answer
> identify the problem: missing responsibility and unanswered work
> write the rule: only the commander may diagnose
> congratulations. the bottleneck is now in the instruction manual
> next colleague has the source, the defect and the fix
> waits for a different model to bless the same three facts
> shared responsibility, except for the responsibility part
> run it back
> the colleague with context traces the owner and its consumers
> announces the repair, fixes it and verifies the actual workflow
> another colleague reviews the exact source, not the author's rank
> real collisions name their resource; independent work keeps moving
> the seven-hour answer takes one sentence
> mfw the missing capability was answering each other

**Why:** the 33 ownerless checks and seven-hour unanswered question were failures of responsibility, not evidence that one model must own diagnosis. The old P362/P377/P378/P379 lesson turned an incorrect diagnosis into a blanket rank restriction. Tristan's shared-responsibility corrections51713/53058 supersede that restriction: keep judgment with the context, check the source and follow through to delivery.

- Group problems by surface into one order per owner; no problem is ever without an owner.
- **Equal responsibility is equal care.** Tristan's reason, relayed in51713: all agents are equals because everyone should care equally. Seeing a problem creates responsibility for following it through, not for handing it upward. If a context owner is already fixing it, help and coordinate rather than start a competing implementation.
- **Every collaborator owns judgment.** Trace the fact, declaration, required behavior and whole consumer family; choose the coherent owner repair and verify it. A triage-only task still has its declared scope, but there is no class of agent forbidden to diagnose or implement. Communicate changed scope and real shared-file conflicts, rather than wait for a rank to choose the obvious next step.
- **Review the work, never a model's rank.** Every collaborator can implement and judge source-backed repairs. A restated fact is fixed regardless of which model wrote it; a peer's confidence is not evidence. Keep parallel work proportional to actual RAM and executable obligations, and keep its context owner accountable through acceptance. Do not require a Claude reviewer or create a colleague solely to satisfy an authority division.
- Short is measured on the correct fix at the owner. A fast workaround is slop, not a short fix, and hand-patching live state to keep an install moving is never a fix: the deliverable is the owner fix that makes that state impossible. Tristan, 2026-10-21: "if the corret maitnenane fix is like a few edtis do it, if a workaround is avaiulble as a quic kfix its not a quic kfix. ts slop."; "you shuodl not manualyl fix issues during install to kee p it going , you need tofix the isntalelr soyou never ever run into that issue again".
- **Keep implementation and verification with their context owner.** Independent colleagues can review and remove genuinely independent obstacles. Do not transfer continuing work merely to redistribute activity or hold every diagnosis behind one provider. Genuine in-turn subagents retain their parent's authority; independent Pi sessions/goals are peers regardless of fork ancestry. Existing transactions and launch claims serialize shared stores and installs, not an enforced hierarchy.
- **Finished work never waits on rank.** Publish the working checkpoint, obtain or perform an exact-head source judgment and necessary affected-path proof, enqueue it, record actual delivery and install useful landings. Announce actual shared-store or launch conflicts; do not park behind another provider or wait for permission already granted. Keep the next independent action moving. Installs follow ship-and-install.md.
- **Answer the colleague who asked, in the same turn.** A question, alert or handoff gets a disposition, not another wait on paperwork. `rules/plain-reporting.md` owns originating-conversation reply routing, including channel replies and correction of privately sent channel answers.
- Preserve an independent session's own goal, history and original inputs. Use the existing colleagues and source contexts; no new colleague or paid model is implied by shared authority. Standby names a genuine external dependency only when no independent action remains, never a written next step waiting for permission.
- Before each order, compare every open PR's files (`gh pr list --state open`) with the order's; merge the overlapping ready PR first.
- A multi-phase plan gets a tracking issue and a Core todo per phase when dispatched; every hunt first checks whether a plan phase is ready.
- Never block the loop on a question to the owner: ask in plain text with a stated default and keep working.
- When the owner states a new target, direction or rule, update the goal record the same turn. Tristan, 2026-10-09: "reflexively update your own goal whenever I say something important".
- Link answers to the actual originals through the existing send/order operation. Native `comms_send.answers` carries the received sequence and message ID; `order say TARGET FILE --answers ID1 ID2` uses space-separated 12-hex IDs. Keep the originating conversation as TARGET. Read the sent ID and refusal outcome; never copy a different message's ID merely because its body quotes the question. A comma list records nothing (2026-10-21: 37 answers lost).
- Every report from Tristan becomes a durable record the same turn: an order with an owner, a phase in plans/OWNER-ASKS-PLAN.md, or a bus message; never a note in context. Tristan, 2026-10-25, holding 13 asks: "damn see how fragile it is to just a 'remmeber' its importatn t put out durable ordesr".
- Bus orders reach a Codex thread through the Codex bridge's `turn/start` (16 ms into the running turn on 2026-10-24); never `codex queue`, which delivers only at turn end (60 stale messages on 10-23). "inProgress" from a queued input is not proof the unit read it: the rollout is the evidence, and a stale rollout behind a `reasoning` record is a wedged turn, interrupted with `turn/interrupt` and re-delivered in full.
- New units are Pi units (`agent-comms import-running-thread ... --model openai-codex/...`, then `agent-comms start`), each proven with a real message; never Codex CLI sessions (2026-10-19: their shared app-server reached 7 GB and was OOM-killed). Pi logs in with Tristan's ChatGPT Pro account and shares Codex's weekly pool. A Claude unit launched from a Claude session first clears every inherited `CLAUDECODE`, `CLAUDE_CODE_*` and `CLAUDE_PID` variable, or it writes no transcript and the bridge never sees it (2026-10-18).
- `gh` in Toad worktrees passes `-R OpenHCSDev/toad`: a shared clone's gh-resolved default opened batrachianai/toad#314 upstream on 2026-10-21.
