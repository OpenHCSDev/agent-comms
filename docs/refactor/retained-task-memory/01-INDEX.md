# Feature completion index

**Source reviewed:** `4295d680`.
**Rules:** [00-RULES.md](00-RULES.md). **Evidence:** [04-EVIDENCE.md](04-EVIDENCE.md).

**2026-10-01 checkpoint:** this table is the original reviewed-head baseline.
#475 delivers the named partial S2 foundation and paired S5 source and removes
the dormant prototypes; full S2 remains open in #481. See
`docs/checkpoints/s2-foundation-s5-source-20261001.md` for delivered scope and
actual acceptance boundaries. S1 is not completed by deleting its prototype.

## Current versus intended behavior

| Surface | Original intended answer | Source-backed state at this head | Completion target |
| --- | --- | --- | --- |
| [S1](S1-TIMING.md) timing | Is now a useful task boundary? | Dormant TriggerRule; live selected decision uses stored-context/usage threshold | Task-boundary decision from source-backed evidence, subordinate to hard limit |
| [S2](S2-MEMORY.md) exact memory | Which exact current facts must survive? | Dormant RetentionPolicy; narrative summary/recent tail/file annotations plus separately persisted state | Revision-bound derived facts from canonical owners carried through the same checkpoint |
| [S5](S5-TURN-CONTEXT.md) turn context | What input does this turn receive, and from which owners? | Coordination and other contributors concatenate strings without a shared provenance manifest | Phase 1: one typed assembler, byte-identical input and inspection; phase 2: authored retained operations after S2 |
| [S3](S3-CACHE.md) prefix reuse | Can this selected route safely reuse prefix/cache? | Separate bounded summary requests; no task-specific cache-preserving route found | Capability-owned prefix strategy with measured provider cache usage |
| [S4](S4-EVALUATION.md) retention | Does the model answer correctly after repeated compactions? | Existing tests prove lifecycle/continuation; no matched four-control recall study found | Shared histories/questions, repeated rounds, exact-state and model-quality reports |

PR48 deferred activation; PR95/135 activated journaled compaction. Complete the
remaining features in the order below.

## Order

1. Land this evidence and scoring scaffold. PR416 output accounting merged during
   preparation and is integrated here; preserve its shared policy instead of
   independently changing that budget.
2. S2 defines source projections/invalidation and typed Decision emission on the wire. S1
   consumes the same revision evidence for boundaries. Sequence shared source and
   native preparation changes under one accepted implementation owner.
3. S5 phase 1 runs in parallel with S2: preserve every rendered byte while moving
   contributor ownership into ContextSegment/TurnContext, record text-free per-turn
   manifests on the original wire, and expose context inspection. Phase 2 waits
   for S2 retained classes, then integrates RetainedSegment and authored operations.
4. S3 can investigate selected-route capability and external cache behavior
   independently, but request native execution changes from its current owner.
5. S4 runs store/native controls first, then authorized matched-model evaluation
   of the combined candidate. Do not label fixture tests as recall validation.

## Crossings and existing PRs

| Shared area | Resolution |
| --- | --- |
| `owner_compaction_adaptive.py`, preparation/source/witness and selected RPC | S1/S2 request changes from one accepted owner; no second route |
| `CompactionPolicy`, native summary generation/accounting | S2/S3/S4 share owner; [PR416](https://github.com/OpenHCSDev/agent-comms/pull/416) first |
| Pi vocabulary/payload declarations | [PR417](https://github.com/OpenHCSDev/agent-comms/pull/417) merged and integrated in the followthrough worktree; use its typed response owners |
| state/commands and turn lifecycle | [PR421](https://github.com/OpenHCSDev/agent-comms/pull/421) and [PR425](https://github.com/OpenHCSDev/agent-comms/pull/425) are merged in refreshed main; extend their original state/lifecycle owners, not retired mirrors |
| native preparation and shared compaction scheduling | Merged [PR439](https://github.com/OpenHCSDev/agent-comms/pull/439) owns shared admission across independent summary sources; retention must preserve that existing source/custody contract |
| retained native fixtures | Reuse `tests/retained_native_fixture.py`, selected-owner integration and manual-compaction journeys |
| synthetic recall oracle | S4 owns fixture/measurement, never runtime selection or task authority |

## Decisions with reversible defaults

| Decision | Default |
| --- | --- |
| Task-aware probes and their provider overhead | Opt-in until matched measurements justify activation; hard-context protection unchanged |
| Source of exact facts | Derive from existing owners with source revisions; no new authoritative memory database |
| What to preserve first | Verbatim applicable constraints; genuine choices with valid rejected alternatives; exact source-owned references; narrative only for remaining context |
| Peer-message provenance | Project original wire/message identity and authority; never reconstruct another author's constraints or claims from injected transcript prose |
| Decision provenance | S2 owns the wire Decision and comms_decision tool; original author/turn are bound by admission, never inferred from narrative |
| Revision/lock-in gates | Zero unauthorized constraint/Decision drift; sanctioned corrections separately reported; held-out probes require previously unnecessary distinctions |
| CI queue | Does not hold progress or useful verified checkpoints; enforced merge rules still apply |
| Unsupported cache route | Select bounded strategy before any provider work; never replay after an uncertain send |
| Paid evaluation | No calls until Tristan authorizes model, sample count and spend cap |
| Quality threshold | Zero exact-state drift/invalid actions; S4 paired recall lower bound >= -2 points, cost/p95 improve >= 10%, S1 local journey budgets unchanged; register before results |

Paper4b's four-class retention and lock-in design governs S2/S4. Source findings
are pinned in [04-EVIDENCE.md](04-EVIDENCE.md); audit outputs live in
[PR428](https://github.com/OpenHCSDev/agent-comms/pull/428).
