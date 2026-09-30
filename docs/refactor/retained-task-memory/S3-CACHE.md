# S3: Cache-preserving summary capability

**Source reviewed:** `4295d680`.
**Rules:** [00-RULES.md](00-RULES.md). **Step 3. Origin:** PR48 proposal.
**Shared abstractions** ([02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md)). *Builds:* none. *Uses:* selected transport, native strategy and outcome owners.

## Gap and source witnesses

**The selected compaction path makes separate summary requests.**
Native776 uses fresh summarization routing and cacheRetention none. The selected
RPC calls native compact through the already selected stream, without a separate
client or credential resolver. CompactionPolicy owns bounded map/reduction.

## Required questions and relation

Does this actual selected transport support prefix-preserving summarization? What
exact ordered messages, instructions, routing identity, tool/hook exclusion and
usage semantics does the external API require? How is support established before
spending? What measured cache usage and total cost/latency result?

Required: transport capability -> allowed request form; selected
source/witness -> unchanged prefix; CompactionPolicy -> packing/budget;
provider usage -> cache measurement; existing journal/outcome -> settlement.
Forbidden: model-name string heuristic -> support (MEMB-2); matching prefix text ->
asserted cache hit; failed/uncertain request -> automatic alternate strategy/replay.
Declare support per external route.

## Ownership

Extend the actual provider/route capability declaration and existing native
strategy planning seam. A capable route owns request formation; unsupported
routes select the existing bounded strategy before provider work. Do not fork
Pi's transport, copy SDK cache settings into ACP, or add a second credentials/
request pipeline (IMPL-13, TIME-7). Keep provider usage/cost and retained-output
accounting distinct; PR416's merged shared check is integrated here, not duplicated.

## Route decisions and defaults

| Question | Decision and default |
| --- | --- |
| Supported API and route catalog | The selected provider/transport declaration supplies the capability and exact API. Absent an implemented capability, choose bounded summaries before sending. |
| Hooks, tools and auth effects | Use existing native transport/auth; summary requests exclude action-producing tools and reuse current approved hooks. A route unable to enforce that uses bounded strategy. |
| Selected model limits | Read effective limits from the selected settings/transport owner; never copy defaults into ACP or match model-name strings. |
| Prefix equivalence | Preserve the original ordered prefix and append only declared summary instructions. Source mismatch refuses preparation/commit. |
| Cache lifetime | Use the provider's documented route lifetime. Unspecified lifetime means no assumed reuse or savings. |
| Usage accounting | Record original per-response cacheRead/cacheWrite and billed input/output/reasoning usage; missing cache fields are unavailable, not inferred hits. |

Measure cache performance on each admitted route.

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
Done requires an implemented capability and measured gains. Same-text and
fake-stream cache assertions cannot establish provider cache behavior.
