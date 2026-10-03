# Original compaction preparation and request timing

Original469 boundaries summary committed at+144.265s before TRIAGE+146.666s.
Its relevance/answer request spans total7.305s. The source has no retained dispatch
or first-event clocks for the selected summary, so historical local/provider
attribution remains unknown. No repeated provider wave can recover those clocks.

Extend the existing NativeRequestObservation and completeSummarization operation,
including source/prefix and concurrent leaf calls. Use the original session's
request callback. Put first-delta consumption on that same request resource and
remove the native loop's duplicate observation code.

PiEvent's existing observation hook consumes a recording callback. Both the raw
turn and SelectedSummarySlot use it. The slot records actual model request clocks
under its existing journal operation, original turn lease and acquired native
process into the existing request diagnostic stream. Summary progress and terminal
response keep their original identity, no-byte, UNKNOWN and retirement rules.

TrackedTurnSession's existing acquisition measurements own the full context
preparation span. No new timer, phase, cache, store, input, replay or provider call.
No runtime journal codec/schema or old source proof changes. Source first; complete
batch checks last. Sch owns a future reviewed native artifact and public receiver;
this source batch cannot claim an installed native timing path from old960.

Same checkout after normally receiving554. Shared native claims requested from
Sch, Python lifecycle claims requested from Arendt; no competing implementation.
Catalog: IMPL-12, BOUND-2; original diagnosis and proof remain under554 evidence.
