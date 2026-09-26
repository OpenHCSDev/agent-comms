# Adaptive compaction and retained task memory

**Status: draft plan — pending implementation.** This document introduces no
runtime behavior, provider calls, or configuration switches. Implementation is
deferred to a separate change after the current fixes in [PR #47](https://github.com/OpenHCSDev/agent-comms/pull/47)
land and are installed. That release does not depend on this plan.

## Evidence and intended improvement

[SelfCompact, v2, §3 and Algorithm 1](https://arxiv.org/html/2606.23525v2#S3)
pairs a compaction tool with a task-specific rubric: favor completed subtasks
and suppress interruptions during unfinished reasoning. Its probe and summary
instructions append to the existing prefix to reuse cache where the serving
engine supports it. The reported search cost reductions compare with a
no-compaction baseline; they are not wall-clock guarantees for our stack.

[Context Compaction Theory, v1, §§4.3–5](https://arxiv.org/html/2608.01326v1#S5)
relates retained information to future queries under specified query regimes.
It leaves repeated-compaction error and efficient realization open. Our
engineering response is to retain exact task state outside narrative summaries
and measure retrieval across repeated compactions.

[Agarwal's compression experiment](https://www.rajan.sh/llm-compression)
compares learned gist plus recent context with recent context alone. It trains
both compressor and decoder; its results do not establish retention guarantees
for our configured, unmodified provider models. The useful evaluation idea is
the recent-context-only control.

## Proposed ownership and interfaces

These interfaces are proposals, not implemented APIs. Reuse the declaration-owned
`CompactionPolicy` from the current fixes after they merge. Put accepted fields,
defaults, validation, and available implementations at their owning declarations;
derive adapter configuration and catalogs from them. Avoid copies in Python,
native patches, ACP, and Toad.

| Proposed boundary | Responsibility | Authority it must preserve |
| --- | --- | --- |
| `TriggerRule.evaluate(snapshot)` | Return skip or a compaction candidate, with source cursor and bounded evidence | The runtime decides admission; a model verdict never grants goal authority |
| `RetentionPolicy.select(snapshot, budget)` | Select exact task facts, source references, and a recent window | Registry, goal/attempt ledger, native transcript, and claims stores remain canonical |
| `SummaryStrategy.plan/execute(input, budget)` | Produce a candidate summary and per-response usage | Selected model, route capabilities, output reserve, and no-replay rules |
| Existing compaction commit owner | Recheck authority and session revision; durably commit a complete result | Send-boundary lock, session writer fence, stop, and no partial commit |

A runtime snapshot reads current owners rather than maintaining another mutable
registry. Task memory is a derived view with identifiers, revisions, and evidence
references. It must not turn an inferred summary into an authoritative goal,
claim, completion verdict, or input disposition.

## Implementation sequence

1. **Define contracts and fixtures.** Declare the interfaces and configuration
   once. Capture representative coding, research, and long-running goal traces
   with expected answers and exact evidence. Preserve the existing hard context
   backstop, tool-call/result pairing, hidden grants, and one in-flight goal
   attempt. Invalid configuration fails before provider work.
2. **Add an adaptive trigger.** Introduce task-specific completion/unfinished-work
   rubrics at declared decision boundaries. Bound probe cadence and include its
   usage in the attempt. A skip leaves transcript and task state unchanged.
   Stale evidence is discarded; it cannot authorize compaction across a changed
   turn or goal. Approaching the actual context limit still invokes the existing
   overflow-prevention path; a rubric cannot waive that limit.
3. **Add cache-preserving summaries where supported.** Model this as an explicit
   route capability. Preserve the ordered prefix and append summary instructions
   through supported APIs. Measure reported cache usage; do not assume a cache
   hit from identical text. Unsupported routes use the existing bounded strategy
   selected before sending. Failure after an uncertain send never causes an
   automatic fallback or replay. Do not fork Pi's transport.
4. **Add retained task memory.** Project active goal/status, latest user
   corrections, queued-input identities and dispositions, unresolved failures,
   exact symbols/paths/hashes, and relevant evidence references from their owners.
   Preserve the recent window and outstanding tool pairs. Narrative summaries
   carry context around those facts. Deletion, rename, completion, and correction
   must invalidate stale derived entries without resurrecting old authority.
5. **Evaluate before deployment.** Freeze implementation commits and fixture
   inputs, exercise the real Toad → ACP → native Pi path, and compare the same
   models and histories against the current bounded strategy. Publish results
   and limitations before changing installed owners.

## Acceptance gates

- At least three sequential compactions with goal continuation, queued user
  input, a later correction spanning summary segments, exact identifiers, and
  an unresolved failure. Test a repeated split turn with an existing summary
  and no new history messages. Previous summary and custom focus must survive.
- Fixed held-out questions after every round: exact task facts must be available
  through authoritative storage; measure the model's retrieval accuracy
  separately. Compare full context where it fits, current bounded compaction,
  adaptive compaction, and recent-context-only controls. Do not equate source
  coverage or a mocked summarizer with semantic retention.
- Controlled failures for stop, goal replacement, writer races, interrupted
  probes/summaries, failed synthesis, and restart. No partial commit, hidden
  instruction loss, automatic uncertain replay, or duplicate usage attribution.
- Live measurements on isolated copies using configured models: phase latency,
  total latency, input/output/cache usage, summary size, and post-compaction
  answer quality. Report sample sizes and distributions, not just the fastest
  run. Account for probe overhead. Adoption requires a measured improvement
  without regression in exact-state controls; define quality margins before
  seeing comparison results.
- Verify bounded startup/reopen work against discarded transcript length and
  thread count. Do not promise constant work for unlimited active context.
  Test installed dependency pins and the visible progress/diagnostic path.

No learned compressor training, unsupported provider-native route, universal
losslessness guarantee, or general plugin framework is part of this first
implementation. The interfaces above leave room for additional declared rules
once their behavior can be tested against the same acceptance gates.
