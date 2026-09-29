# S1: Task-aware compaction timing

**Head audited:** `697bba42f5f03e169ff8eae9490090cbd0d0b89e`.
**Rules:** [00-RULES.md](00-RULES.md). **Step 2. Origin:** PR48 proposal.
**Shared abstractions** ([02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md)). *Builds:* none. *Uses:* existing native decision/source/commit owners.

## Gap and source witnesses

**The selected live decision answers a context-limit question, not a task-boundary question.**
`stack/native-compaction-selected-summary.mjs:28-46` uses storedContext and
shouldCompact; `owner_compaction_adaptive.py:136-158` consumes that decision.
Dormant `TriggerRule` needs a completed-subtask marker and matching source
reference, but nothing projects those from the current owner/transcript.

## Required questions and proposed relation

- Is a subtask complete, reasoning unfinished, or evidence unavailable?
- Is that observation still bound to the same source, turn and latest correction?
- Is the hard context backstop due regardless of a rubric?
- Does a prospective probe cost enough to erase the benefit?

Provisional required: boundary evidence -> task-aware decision, selected effective
settings -> hard-limit decision, revision witness -> eligibility at preparation
and commit. Forbidden by the original spec: model rubric -> goal/claim authority;
unfinished/unknown boundary -> forced optional compaction; task skip -> waive
hard-context protection. A completed assistant utterance alone is not proof the
whole goal is complete. Those are independently varying meanings (IDEN-1).

## Candidate owner and counterevidence

Extend the existing selected decision/preparation contract to consume a bounded
source-backed boundary observation. Keep the budget owner in Pi and source checks
in the existing commit boundary. Do not independently calculate thresholds in a
new TriggerRule adapter (TIME-7), or add a central switch for boundary behavior
(IMPL-1). A behavior family is justified only if actual cases need independently
owned behavior; do not invent subclasses just to represent three value labels.

OPEN: canonical completion evidence versus heuristic probe; probe cadence and
cost; mid-turn/split-turn eligibility; effects/aliases and all native callers.
Admission requires native and contextual source receipts. Task-aware timing is
opt-in initially as a reversible spend/behavior default. No second pipeline.

## New-case experiment, deletions and guards

Add an interrupted-subtask case. Its decision semantics should be declared at the
boundary owner once; source/commit/native/UI consumers should need no new switch.
Count actual edits before and after implementation. Delete displaced dormant
TriggerRule and its tests when equivalent production contracts are integrated.
Guard against copied thresholds and optional timing bypassing the hard backstop.

## Tests and done when

Use the existing selected native fixture/localhost transport. Prove completed
boundary permits optional compaction, unfinished/unknown skips, correction makes
evidence stale, hard limit still compacts, and stop/goal replacement/source race
preserve original input and UNKNOWN state. Compare probe cost and latency in S4.
Done requires the same native path, all callers migrated, guards and behavioral
checks, plus measured benefit before default activation. Dispatch after S2's
source contract is accepted; no production owner assigned by this draft.
