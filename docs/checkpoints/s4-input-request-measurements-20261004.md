# S4 original input request measurements

The existing RecordedNativeProbe reader decodes the original per-turn diagnostic
stream once but discards requests other than the final SDK manifest's request.
Tool-step request budget/timing observations can exist in that same source while
the report shows only the final selected request. Original journal usage already
counts every assistant completion; diagnostic and usage scopes must stay separate.

Keep acquisition, identity and ordering with this existing probe. Read original
RequestProgress through its codec, require the recorded turn fence/native session
and input, then group those values by their original request IDs. Preserve the
selected manifest's exact request separately for its SDK/model/terminal joins.
Timing and budget readers consume the same acquired values; no repeated decode,
source scan, clock subtraction, reconstructed observation or native change.

All-input reporting means retained diagnostics for this input, not proof every
request/retry was recorded. Optional diagnostic omissions remain unavailable;
earlier requests do not acquire the final manifest's digest/SDK coverage. No
whole-turn timing, provider capacity, HTTP, billing or comparative-study claim.

No original/provider/SDK repetition, native/runtime/tool edit, package/loan/env
or new store/class/scanner. Source/AST first, coherent private consumer migration,
then one bounded original JSONL/codec qualification last. The 30-pair/USD75 study is still
unapproved and is not started by this change.

## Scoped Ready

Published implementation2f7f8116. Existing observed_requests owns one acquisition
and original turn/session/input checks; input_request_measurements consumes its
groups through unchanged budget/timing behavior. construction derives only the
manifest-selected request for SDK/model/terminal joins. All singular method
callers are migrated and the final-request acquisition filter is deleted. No
new class/store/codec or production/native/tool change.

Final batch:4 affected controls passed/33deselected in0.37s. They cover original
JSONL and codec, first-seen request/stage order, retry stages/zero counters, missing
capture/budget, foreign input exclusion, wrong source turn/mutation refusal and
preserved selected-request completion/alignment. No original records, SDK or
provider/native process was run. This is source/authored reader qualification,
not a new configured multi-request capture or complete input-wide timing.

AST:734 modules/0omissions, old method definition/attribute-call sites removed;
three remaining singular Name sites are the local selected tuple, not callers.
Original RequestProgress/manifest/diagnostic writers are unchanged. Source/evidence
is in evidence/s4-input-request-measurements-20261004/. Exact hosted Debt must
pass before parent merge; full S4 remains unfinished.
