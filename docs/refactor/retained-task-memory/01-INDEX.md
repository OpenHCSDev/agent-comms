# Feature completion index

**Head:** `697bba42f5f03e169ff8eae9490090cbd0d0b89e`.
**Rules:** [00-RULES.md](00-RULES.md). **Evidence:** [04-EVIDENCE.md](04-EVIDENCE.md).

## Current versus intended behavior

| Surface | Original intended answer | Source-backed state at this head | Completion target |
| --- | --- | --- | --- |
| [S1](S1-TIMING.md) timing | Is now a useful task boundary? | Dormant TriggerRule; live selected decision uses stored-context/usage threshold | Task-boundary decision from source-backed evidence, subordinate to hard limit |
| [S2](S2-MEMORY.md) exact memory | Which exact current facts must survive? | Dormant RetentionPolicy; narrative summary/recent tail/file annotations plus separately persisted state | Revision-bound derived facts from canonical owners carried through the same checkpoint |
| [S3](S3-CACHE.md) prefix reuse | Can this selected route safely reuse prefix/cache? | Separate bounded summary requests; no task-specific cache-preserving route found | Capability-owned prefix strategy with measured provider cache usage |
| [S4](S4-EVALUATION.md) retention | Does the model answer correctly after repeated compactions? | Existing tests prove lifecycle/continuation; no matched four-control recall study found | Shared histories/questions, repeated rounds, exact-state and model-quality reports |

There is no evidence here of a deliberate cancellation of the original features.
PR48 explicitly deferred activation; PR95/135 subsequently activated journaled
compaction, not the dormant timing/retention contracts. That explains the code
state, not the team's unrecorded prioritization decisions.

## Order

1. Land this evidence and scoring scaffold. PR416 output accounting merged during
   preparation and is integrated here; preserve its shared policy instead of
   independently changing that budget.
2. S2 defines retained-fact derivation and invalidation on existing stores. S1
   consumes the same revision evidence for boundaries. Sequence shared source and
   native preparation changes under one accepted implementation owner.
3. S3 can investigate selected-route capability and external cache behavior
   independently, but request native execution changes from its current owner.
4. S4 runs store/native controls first, then authorized matched-model evaluation
   of the combined candidate. Do not label fixture tests as recall validation.

## Crossings and existing PRs

| Shared area | Resolution |
| --- | --- |
| `owner_compaction_adaptive.py`, preparation/source/witness and selected RPC | S1/S2 request changes from one accepted owner; no second route |
| `CompactionPolicy`, native summary generation/accounting | S2/S3/S4 share owner; [PR416](https://github.com/OpenHCSDev/agent-comms/pull/416) first |
| Pi vocabulary/payload declarations | [PR417](https://github.com/OpenHCSDev/agent-comms/pull/417) merged and integrated in the followthrough worktree; use its typed response owners |
| state/commands and turn lifecycle | [PR421](https://github.com/OpenHCSDev/agent-comms/pull/421) and [PR425](https://github.com/OpenHCSDev/agent-comms/pull/425) own implementation; merged [PR418](https://github.com/OpenHCSDev/agent-comms/pull/418) records dispatch |
| retained native fixtures | Reuse `tests/retained_native_fixture.py`, selected-owner integration and manual-compaction journeys |
| synthetic recall oracle | S4 owns fixture/measurement, never runtime selection or task authority |

## Decisions with reversible defaults

| Decision | Default |
| --- | --- |
| Task-aware probes and their provider overhead | Opt-in until matched measurements justify activation; hard-context protection unchanged |
| Source of exact facts | Derive from existing owners with source revisions; no new authoritative memory database |
| What to preserve first | Verbatim applicable constraints; genuine choices with valid rejected alternatives; exact source-owned references; narrative only for remaining context |
| Peer-message provenance | Project original wire/message identity and authority; never reconstruct another author's constraints or claims from injected transcript prose |
| Decision provenance | Explicitly emitted nominal Decision under one canonical owner; no retrospective narrative inference or second memory store |
| Revision/lock-in gates | Zero unauthorized constraint/Decision drift; sanctioned corrections separately reported; held-out probes require previously unnecessary distinctions |
| CI queue | Does not hold progress or useful verified checkpoints; enforced merge rules still apply |
| Unsupported cache route | Select bounded strategy before any provider work; never replay after an uncertain send |
| Paid evaluation | No calls until Tristan authorizes model, sample count and spend cap |
| Quality threshold | Exact-state/invalidation controls must pass; predeclare recall/cost/latency margins before seeing model results |

No new provider model or automatic memory-mining policy is silently enabled.
The user-supplied Paper4b retention/lock-in translation is incorporated in S2/S4
as intended design and acceptance requirements. Formal proposition/notation
references remain unverified; neither Decision emission nor revision-mass/lock-in
measurement is implemented in this draft. The original table is pinned historical
source evidence, not a fresh audit of the subsequently advanced remote main.

Followthrough at source `452bc01f` is recorded in
[05-AUDIT-REVIEW.md](05-AUDIT-REVIEW.md): all 85 detectors completed for the
declared contextual scaffold scan, with no omissions. The four raw leads are
adjudicated with source counterevidence before any code repair; do not interpret
this as a clean whole-production audit or completed implementation.
