# Summary generation and retained-context budgets

PR520 owns this native policy correction. Arendt owns PR529's source-continuation
barrier; Mendel owns the original provider-composer and startup-latency work.

## Original failure and units

The preserved `/home/ts/wt/s52901/receipt.json` reports provider output 5534,
reasoning 0 and a planned per-request allowance of 4096. Its raw provider usage
is absent. The normalized zero cannot establish whether the provider reported
zero reasoning or omitted the breakdown. No original input is replayed.

`CompactionPolicy.summaryTokens` owns generation intent: selected reserve, model
capability, configured maximum and source ratio yield the allowance for each
leaf request. It is neither accumulated map/synthesis usage nor the final
retained-context size. `ContextBudget` fits explicit SDK allowances at each
provider boundary; absent API options stay absent.

The installed Codex subscription request builder omits an output-cap field.
Public Responses API documentation does not establish that private route's
contract. This change adds no guessed request parameter or transport fallback.

## Existing owners and whole caller closure

The source map uses the installed Jiti/Babel parser on 58 native production
files, with zero parse failures. Declarations, imports and syntactic references
are in `evidence/task-aware-timing-20261002/summary-budget-source/before.json`.
Dynamic provider composition is explicitly not resolved by that AST. Python
source/retention consumers remain covered by the existing source-family mapping.

All history, turn-prefix, map and synthesis requests use
`generateSummaryWithUsage` and `completeSummarization`. The policy now authors
the prompt's generation allowance from the same value passed to SDK options.
The separate residual-context instruction in `compact` is deleted.

`requireSummaryOutput` and its sole caller are deleted. Provider output includes
reasoning; an unavailable reasoning breakdown cannot be subtracted as measured
zero to manufacture a retained-text admission fact. Usage remains original
cost accounting. The existing OpenAI usage decoder preserves absence, and
`combineUsage` publishes a reasoning total only when both operands measured it.

The actual hard retained-context admission remains `packSummary` and
`requireContext`. They measure the composed native summary envelope, exact task
source, file annotations and retained atomic messages with the existing SDK
context estimator. Only narrative may be shortened. Mandatory exact source
which cannot fit is refused. This is unchanged; no limit is raised.

Incomplete, empty, tool-bearing or unshrinking summaries retain their existing
failure behavior. Selected RPC drains actual provider streams and preserves
uncertain disposition; this correction does not authorize a prior attempt.

## Delivery boundary

Source correction precedes validation. A fresh immutable prepared native
artifact is required; native5184 and native7074 remain untouched. Final affected
validation must cover the actual installed summary RPC/packing path, prompt
allowances across map/synthesis and unavailable versus measured reasoning.
No fresh configured-provider fork or replay is needed to investigate this cause.

Patterns: IDEN-1 (usage versus retained size), IDEN-2 (two meanings of summary
tokens), TIME-7 (a second prompt allowance), BOUND-2 (bypassing the context owner).
