# S3 source findings; implementation remains parked

Core census: `24c60646eeab364fd32a8acb4777d40a4d47f865`, based on
main `155b00076312626e025861efc3ed14549adca5c0`. Original requirement:
`docs/refactor/retained-task-memory/S3-CACHE.md`, originally merged through #428.
This checkpoint changes evidence only. It does not implement or accept S3.

## Existing owners and the remaining relation

| Fact | Existing owner and source | Remaining work |
| --- | --- | --- |
| Effective transport, authentication and hooks | Native `ModelRuntime.prepareRequest` (model-runtime.js:422); SDK stream wrapper (sdk.js:194–226) | Keep the original selected stream. Its existence does not establish a prefix capability. |
| API support and route selection | pi-ai `ProviderStreams`; `lazyApi` (api/lazy.js:56), `createProvider` (models.js:431); coding-agent `composeModelProvider` (provider-composer.js:298–376) | Declare lawful summary request formation on the effective route. The composer can select extension streaming before base streaming; base support must not survive a replacement automatically. `withRemoteCatalog` spreads the provider, but the composer constructs a fresh object and explicitly forwards capabilities. Close those consumers together. |
| Exact source and admission | `SelectedSummarySource`, `NativePreparation`, `SelectedSummarySlot.run_selected_summary`; native selected-summary admission | Keep source/witness reservation and original child custody. Prefix context must come from original native session conversion, system prompt and tools, not a Python reconstruction or serialized narrative. |
| Bounded planning, output and cancellation | Native `SummarySource`, `HistorySummarySource`, `CompactionPolicy`, `compact` (compaction.js:573–606) | Choose prefix formation before a request when the effective route and full input budget permit it. Otherwise use the existing bounded plan before spending. Preserve split-turn source boundaries and retained-history packing. Never choose an alternate strategy after uncertain execution. |
| Actual summary request | `generateSummaryWithUsage` (compaction.js:403–483), `completeSummarization` (:348–355) | The former constructs a fresh conversation-tagged prompt; the latter forces cacheRetention none and generates a routing ID if none was supplied. Neither preserves the original request prefix. Removing none alone is insufficient. |
| Cache usage and settlement | API usage decoder; `SummaryUsage`/`SummaryCost`; original native summary response and journal | OpenAI-compatible `parseChunkUsage` (:1193–1220) normalizes absent cache counters to zero. Those normalized zeros cannot distinguish unavailable measurement from an observed zero. Route capability and original response provenance must establish measurement availability; do not infer hits from matching text or timing. |

Provider declaration, effective composition and summary formation are one family
closure (MEMB-2, IMPL-13, TIME-7). No second transport, credential resolver,
capability catalog, cache, usage store or retry path is justified.

The #520 timing/context grant remains disjoint: its settings RPC is version 2
with purpose/boundary. This older census base still has version 1; any future
implementation must integrate that source normally, never restore version 1.
Einstein owns timing/source-budget conversion, Arendt summary lifecycle/custody,
and Sch native artifact/pins. Mendel owns the granted S3 strategy/capability seam.

## Source coverage and limits

`python-owner-census.json` uses existing NRA `audit.findings.Package.load` across
the declared Core src/agent_comms, tests and tools roots: 11 seed declarations,
173 conservative reference sites in 50 files, no Python parse failures. Spelling
matches are not dynamic receiver or dispatch proof. Other repository roots are
outside this query.

Native provider/API/composer/summary declarations above were read directly from
the unchanged reviewed Native5184 dependency. No JavaScript AST grammar was
available in the existing environment; native AST coverage is **incomplete**.
This checkpoint does not use that omission to justify semantic source edits.

Exact dependency file SHA256s:

- pi-ai models.js: `42610d47fe293d99f4b05b147971e181c7312ea47c9be2906a4803955276a8a4`
- coding-agent provider-composer.js: `8eca507009d00768130e46cd9a0831e4fa2f98b81435ae0a8d5079a1d27e13cd`
- coding-agent compaction/compaction.js: `2e507c23edf47391265f81db353b205d9f5e9b78b3025f0497e9415a6121d2de`

No environment, worktree, native build, test or provider call was created for
this census. No actual cost, cache gain, latency gain or model recall is claimed.
Real-provider comparison remains dependent on an explicit budget grant.
#528's existing 339-file installed proof and private gate were handed to Sch for
the #528/#525 pair; parent owns its next configured-channel acceptance.
