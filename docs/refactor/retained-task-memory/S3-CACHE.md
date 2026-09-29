# S3: Cache-preserving summary capability

**Head audited:** `697bba42f5f03e169ff8eae9490090cbd0d0b89e`.
**Rules:** [00-RULES.md](00-RULES.md). **Step 3. Origin:** PR48 proposal.
**Shared abstractions** ([02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md)). *Builds:* none. *Uses:* selected transport, native strategy and outcome owners.

## Gap and source witnesses

**The verified selected compaction path makes separate summary requests, not the proposed prefix-preserving query.**
Native776 uses fresh summarization routing and cacheRetention none. The selected
RPC calls native compact through the already selected stream, without a separate
client or credential resolver. CompactionPolicy owns bounded map/reduction.

## Required questions and proposed relation

Does this actual selected transport support prefix-preserving summarization? What
exact ordered messages, instructions, routing identity, tool/hook exclusion and
usage semantics does the external API require? How is support established before
spending? What measured cache usage and total cost/latency result?

Provisional required: transport capability -> allowed request form; selected
source/witness -> unchanged prefix; CompactionPolicy -> packing/budget;
provider usage -> cache measurement; existing journal/outcome -> settlement.
Forbidden: model-name string heuristic -> support (MEMB-2); matching prefix text ->
asserted cache hit; failed/uncertain request -> automatic alternate strategy/replay.
This is an external route capability, not a global promise every model supports it.

## Candidate owner and counterevidence

Extend the actual provider/route capability declaration and existing native
strategy planning seam. A capable route owns request formation; unsupported
routes select the existing bounded strategy before provider work. Do not fork
Pi's transport, copy SDK cache settings into ACP, or add a second credentials/
request pipeline (IMPL-13, TIME-7). Keep provider usage/cost and retained-output
accounting distinct; PR416's merged shared check is integrated here, not duplicated.

OPEN: actual supported API, route catalog, hooks/auth effects, selected model
limits, request equivalence, documented cache lifetime and usage accounting.
Inspect current Pi provider sources/docs for each admitted route before prescribing
classes or request changes. The historical external study is motivation, not a
cache-performance guarantee for this stack.

## New-case experiment, deletions and guards

Add one supported provider route. Support and formation should change in its
transport declaration, not in Python/native/Toad name rosters. Measure actual
edit count and keep unsupported-case negative controls. Remove any superseded
strategy implementation; do not preserve a failed-send fallback. Guard transport
ownership and no model-name dispatch outside its declared boundary.

## Tests and done when

Localhost adapter tests prove exact prefix ordering, summary-only behavior,
capability selected before send, unsupported skip, cancellation/source race and
no replay. Authorized real-provider comparisons report cacheRead/cacheWrite,
billed input/output/reasoning, latency distributions, request count and recall.
Done requires actual capability and measured gains, not same-text or fake-stream
cache assertions. No paid calls or owner assignment is part of this draft.
