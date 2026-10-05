# Separate arm source/summary/workflow producer

Source and authored orchestration checkpoint for #683. No original-record,
SDK/native, holder, provider, input, package/environment/build or study operation.
USD75/30pairs remains unapproved; full S4 is unfinished.

## What changed

The existing `--paired-construction` runner acquires one untouched, zero-input
SDK seed through `configured_saved_agent`, then uses that same resource owner
separately for each ordered arm. `run_arm` owns its private wire/USER publications,
source inputs, chronological cuts, selected probe children and joined cleanup.
The seed is the same starting native source; each arm executes the frozen source
and probe text separately. Existing design/oracle ordering and manual-cut/probe
protocol are unchanged, including the full-history constructor when selected.
This does not introduce a no-compaction baseline or optional policy intervention.

Deleted the single shared source/cut loop feeding both conditions and the shared
workflow receipt assigned to both arm records. Each arm's original receipt now
owns its actual monotonic acquisition-to-cleanup span and input membership;
`RecordedNativeProbes` borrows that receipt. The pair and seed receipts are not
arm clocks/costs. Seed lifetime includes nested arms and is not called setup-only,
divided or subtracted. Scoring remains after cleanup and outside these clocks.

The existing `selected_native_fork` / `retire_selected` resource retains source,
config, admission, custody and restoration authority. No application method,
Core/native/lifecycle/codec, original reader, completion-accounting or p95 decision
was replaced. Fork ancestry and original per-edge prefixes still corroborate
source delivery; actual completions/input memberships determine exclusivity.
Completed arm records remain if a later arm fails. Failed/uncertain roots are
preserved and never resumed by this command.

## Source and checks

Existing refactor-audit `Package` parses production/tests/tools before and after.
`SOURCE-BEFORE.json` / `SOURCE-AFTER.json` record omissions and lexical consumers;
dynamic callbacks were read at their owning resources, not inferred from names.
`SOURCE-QUALIFIED.json` pins source and raw keeper hashes. Existing application
function AST and runtime/native/tools/reader/scorer source are unchanged.

Four authored checks pass: separate arm roots with unchanged seed/order/plan;
preservation of the first arm on second-arm failure; completed clock only after
agent and inspector join; no completed clock after cleanup refusal. These execute
selected original function declarations with controlled acquisition boundaries.
Zero-round clock controls establish lifetime only, not actual source/cut/probe
execution. Host lacks ACP and pytest-xdist; initial import/argument refusals and
authored fixture corrections are retained. No package was installed to run them.
Full module/installed imports are unqualified. No accepted historical controls or
original records were repeated. The bounded authored scratch is disposable after
its raw logs are retained; existing source/evidence/UNKNOWN remain protected.

## Remaining work

This command changes future execution: each arm now needs its own source inputs,
summary cuts, recall inputs, private roots and cleanup. An old same-cut two-arm
purpose does not authorize the expanded workflow. A genuine configured purpose
must name these new operands/counts and corroborate independent original fork,
source, completion and clock membership. No such purpose/run is started here.

No old report is repaired and no new resource/p95 improvement is claimed.
Additional unretained retries/summary work, complete capacity/transport,
intervention, registered margins and matched study acceptance remain unavailable.
The separate study remains unapproved; this source checkpoint grants no spending.
