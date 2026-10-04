# S4 original request timing consumer

Continue full S4 after merged626 in the same checkout. The existing original
diagnostic producer already records native request timing. `RecordedNativeProbe`
verifies that file and its original manifest/request/lease/session/input, but
keeps only budget-admission rows. Complete transport and callback observations
are discarded at this measurement boundary.

Move that acquisition/correlation to the existing probe owner once. Original
`RequestProgress` values then feed budget selection, native timing and terminal
model corroboration. Migrate all callers and delete the old fused read/filter.
No native/runtime policy, clock, store, lifecycle or provider work is added.
Native timing is not whole-turn/S1 timing or a provider-capacity attribution.

Before editing, existing AST parsed src311/tests365/tools53 with no omissions.
The only budget consumers are probe construction and its existing measurement
controls; scenario alignment consumes their projection. The producer is existing
`diagnostics.record_request_progress`, called by `TurnProgress`, private send and
selected-summary owners. `RequestProgress` owns native clocks and cumulative
callback counters. Local publication/acquisition observations use another clock
and will not be subtracted from native timestamps.

## Published source and final qualification

Implementation `1859e10b` deletes 14 lines and adds 62 across the existing private
reader and its controls. `src/`, `tools/` and `stack/` are unchanged. No new type,
state, store, clock, decoder or lifecycle was introduced.

`RecordedNativeProbe.observed_request` now owns the single original file
acquisition and request/lease/session/input correlation. `request_budget`
selects original allowances from that acquired tuple; `request_timing` exposes
the complete selected request's original `RequestProgress` values. Construction
passes the same objects to both consumers and to the existing terminal-model
corroboration. All direct callers migrated; no old-signature adapter remains.
The public scorer already encodes the construction through FieldCodec, so its
native record and comparison exports carry this evidence without another read.

After implementation the existing AST parsed the same 729 modules, zero
omissions. Acquisition/budget/timing calls are only in the probe's construction
and its measurement control. Scenario alignment and request completion consume
the unchanged budget projection. Native diagnostic producers are untouched.
Lexical references are source evidence, not proof of dynamic resolution.

Three final affected controls passed in 0.24 seconds. They cover exact original
request/turn/session/input correlation, acquisition once across both consumers,
preserved zero callback counters, ordered stages/allowances, missing timing,
and unchanged original admission/terminal model matching.

One bounded read of the existing602 pinned probe/manifest/request diagnostic
metadata passed. Original probe SHA `fff818b4…` matches the previously accepted
record. The same request supplied 15 points (one budget admission), including
preparation, connection, dispatch, first event/delta, stream end, callbacks and
finished. The original last elapsed value is 3230.596397ms, callback total
5.413746ms across15 callbacks. These are that selected request's native
measurements, not the full configured journey duration or a provider-capacity
claim. Manifest and diagnostic SHA values remained unchanged.

Raw receipt: `.artifacts/s4-recorded-request628/original-request.json`.
Published bounded evidence:
`evidence/s4-recorded-request-timing-20261004/original-request.json`.
No original native journal, SDK context or input proof was reread; no native,
model/provider, package, holder or public operation ran. No completed623/624/626
qualification was repeated.

The30-pair/USD75 study remains unapproved and unstarted. Complete submitted
condition construction, sampling, billed resources, end-to-end/S1 margins and
full S4 remain unfinished. Missing historical stages stay absent. Earlier
tool-step requests, summary work, queue/preparation and local publication timing
must not be inferred from the selected request's clock or added across clocks.
