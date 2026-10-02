# S3 source checkpoint

Singer owns existing PR527 after Mendel's explicit handoff. The branch normally
integrates main; no separate worktree, environment or native build was created.
The original Python owner census remains historical source evidence, not a claim
that its old settings-RPC version is current.

## One owner per fact

| Fact | Existing owner and complete consumers |
| --- | --- |
| Allowed external request | Original Codex API `summaryPrefix`, using its own existing endpoint constant. It retains ordered instructions/messages/tools, appends declared summary instructions, and uses its existing `toolChoice:none` serializer. Other endpoints/routes make no support claim. |
| Effective route | `createProvider.apiFor` and `composeModelProvider.routeFor` each supply both their original streaming and prefix methods. Named extension streaming has no prefix capability; a complete NativeProvider may declare its own. No copied support survives a replacement stream. |
| Capability projection | Existing `lazyApi`, API registration, built-in registration, ProviderStreams and Provider/ApiProvider declarations. Registration carries declared capabilities; the duplicate `wrapStreamSimple` identity check is deleted and both streams plus prefix use the original shared wrapper. Provider object spreads remain derived views. |
| Original ordered source | `EntryMessageRange.prefixMessages` traverses original `EntryStore.contextMetadata`, retaining the previous compaction before kept history and stopping at the sealed source end. It converts original entries through `sessionEntryToContextMessages`. |
| Native instructions/tools/converter | `SessionContext.sourceContext` supplies the same envelope to sourceBudget and `prefixContext`; prefixContext uses the selected agent's converter, including configured image exclusion. SourceBudget retains its original converter and ContextBudget calculation. |
| Affinity | The original selected-summary binding retains the registered Provider reference, alongside its existing source/catalog/stream witnesses. Existing acSummaryCompatible compares that reference with the same ModelRuntime owner; the capability uses the bound original provider. No second registry or state is written. Context hooks select bounded formation before sending. |
| Admission and bounded form | `HistorySummarySource.requestContext` selects an admitted prefix or its own boundedPrompt before the leaf scheduler enters auth/provider work. Oversized sources still use the original map/reduction plan first. TurnPrefixSummarySource inherits the same formation; map/reduction leaves and standalone branches retain their original bounded retention. No alternative is selected after an attempted request. |
| Settlement | Original source progress, provider Usage, terminal joins, output limits, source/witness checks and atomic native/journal commit remain. Cache usage counters are observations, not a capability or cache-hit proof. |

The unconditional retention override and history prompt rebuilding are deleted
from the shared summarizer. Bounded/branch options retain their former `none`
setting at the existing options constructor; the admitted original prefix alone
inherits the native route's retention. This avoids changing another provider's
cache-write spending as a side effect. No model, output/source budget, policy,
authentication, transport, cache, store, registry or summary carrier class is added.

Einstein and Arendt granted these shared methods. Sch's current receiving package
is independently frozen; this feature does not alter it, its originals or default.

## Source evidence and limits

Before/after native maps use the original Node builtin Acorn on 544 JavaScript
modules in coding-agent/dist, pi-ai/dist and pi-agent-core/dist. No JavaScript
parse failures occurred. The after map substitutes only final patch-manifest paths
and the changed original source/SessionContext/RPC helper for AST analysis. It is
not a product overlay or execution. Declarations, member uses, imports and
consumer sites are in native-before.json.gz and native-after.json.gz.

Acorn does not parse declaration TypeScript; changed .d.ts contracts were read
semantically. Other external SDK package implementations are outside these roots.
Spelling references do not prove dynamic dispatch. Frozen stock bundle chunks
also contain older compiled declarations; the configured wrapper selects
modular dist/cli.js, and dist/index.js exports modular SDK/compaction owners.
Those stock outputs and the separate agent-core harness compactor are not claimed
as migrated or unique declarations. The new route formation is one API member;
SessionContext.prefixContext owns a different fact: original native context.

The source-only patch composes with zero fuzz against twelve recorded original
files; every resulting SHA256 matches the authored source. Node syntax, shell
recipe syntax and Python patcher compilation were batched after the coherent
implementation. These detect corrupt hunks/preimages and malformed executable
source; they do not qualify an installed package or a provider result.

## Remaining acceptance

The Native9f12/00c2 artifacts remain unchanged. A matching artifact/pin must be
produced by the existing single native builder, then one affected configured
saved-session/inspection journey must observe this producer and original prefix.
No old artifact or historical reader qualifies the new producer. Keep the PR
draft until that ordinary installed path is verified; no standalone new env,
native build, provider call or cache comparison was run here.

Real cache hits, cost/latency comparisons and recall remain unmeasured. The separate
paid comparison has no budget grant. Missing normalized cache counters must not be
reported as measured zeros or inferred savings. API rationale: OpenAI's original
prompt-caching guide specifies preserving definitions and tool_choice:none:
https://developers.openai.com/api/docs/guides/prompt-caching
