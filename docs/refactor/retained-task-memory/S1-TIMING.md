# S1: Task-aware compaction timing

**Source reviewed:** `4295d680`.
**Rules:** [00-RULES.md](00-RULES.md). **Step 2. Origin:** PR48 proposal.
**Shared abstractions** ([02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md)). *Builds:* none. *Uses:* existing native decision/source/commit owners.

## Gap and source witnesses

**The selected live decision answers a context-limit question, not a task-boundary question.**
`stack/native-compaction-selected-summary.mjs:28-46` uses storedContext and
shouldCompact; `owner_compaction_adaptive.py:131-151` consumes that decision.
Dormant `TriggerRule` needs a completed-subtask marker and matching source
reference, but nothing projects those from the current owner/transcript.

## Required questions and relation

- Is a subtask complete, reasoning unfinished, or evidence unavailable?
- Is that observation still bound to the same source, turn and latest correction?
- Is the hard context backstop due regardless of a rubric?
- Does a prospective probe cost enough to erase the benefit?

Required: boundary evidence -> task-aware decision, selected effective
settings -> hard-limit decision, revision witness -> eligibility at preparation
and commit. Forbidden by the original spec: model rubric -> goal/claim authority;
unfinished/unknown boundary -> forced optional compaction; task skip -> waive
hard-context protection. A completed assistant utterance alone is not proof the
whole goal is complete. Those are independently varying meanings (IDEN-1).

## Ownership

Extend the existing selected decision/preparation contract to consume a bounded
source-backed boundary observation. Keep the budget owner in Pi and source checks
in the existing commit boundary. Do not independently calculate thresholds in a
new TriggerRule adapter (TIME-7), or add a central switch for boundary behavior
(IMPL-1). A behavior family is justified only if actual cases need independently
owned behavior; do not invent subclasses just to represent three value labels.

## Timing decisions and defaults

| Question | Decision and default |
| --- | --- |
| Completion evidence | Use an explicit completed-subtask/source observation from the admitted turn owner. Missing/unfinished evidence skips optional compaction; a model probe only advises that owner. |
| Probe cadence and cost | Default provider probing off. If enabled with a budget, at most one bounded probe per new eligible completed-subtask source revision; charge it to the attempt and skip optional probing when the budget is exhausted. |
| Mid-turn and split-turn eligibility | Default to the existing safe native preparation boundary, after a complete tool-call/result pair and before the next original input admission. Unfinished/split-turn reasoning waits; hard-context protection stays independent. |
| Unsupported effects/aliases/callers | Refuse the unsupported optional path; trace and migrate all native callers before enabling it. |
| Activation | Task-aware timing starts opt-in; S4 owns the default flip. When its predeclared margins and the journeys below pass, the same implementation PR turns the default on. Otherwise S4 posts the measured result to Tristan's queue for a decision: flip anyway, change the design, or drop the feature. |

## Identical user journeys and latency budgets

Extend the existing continuous saved-native/ACP/UI journey, holding history,
provider responses and machine fixed between current bounded and candidate timing.
Change only eligible trigger placement, not feedback, continuation or custody.

| Journey | Behavior held identical | Default acceptance latency budget |
| --- | --- | --- |
| Compaction feedback | Automatic/manual start, advancing progress, summary publication and completion/refusal feedback appear in the same original conversation, including tab return and reconnect. No silent interval, duplicate completion or stale status. | Local admission-to-visible-start p95 <= 250 ms; progress-source-event-to-visible-update p95 <= 250 ms. Candidate p95 overhead versus baseline <= 50 ms for each local interval. |
| Turn continues after compaction | The original pending turn/input continues once, first reply/tool result is attached to its original source, and saved-state reopen preserves reply/history and unchanged unrelated UNKNOWN inputs. Stop/decline/source-race has the same outcome as baseline. | Local committed-checkpoint-to-original-continuation-dispatch p95 <= 250 ms; candidate overhead <= 50 ms. Matched end-to-end first-reply p95 <= baseline + 1 s, with controlled provider time reported separately. |

S4 measures these default acceptance budgets using per-phase and total
distributions, samples, provider latency and probe overhead. If baseline
already exceeds an absolute budget, publish that incident and keep optional timing
off until the existing journey owner closes it; do not silently relax the target.
The hard-context backstop still runs. No duplicate UI route or continuation engine.

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
source contract is accepted.
