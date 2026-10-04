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

Source implementation and final bounded original-record checks are pending.
The 30-pair/USD75 study remains unapproved and unstarted. Condition construction,
sampling, billed resources, end-to-end margins and full S4 remain unfinished.
