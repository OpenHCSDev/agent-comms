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
then one bounded source/CLI qualification last. The 30-pair/USD75 study is still
unapproved and is not started by this change.
