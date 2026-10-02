# Owned attempt observations

Owner Mendel; current base457b1047855752f4d069ee1d39b01e30603efa41.
Original385 saved source and all current public inputs remain unchanged.
Arendt granted DurableTurn/AttemptStore/selected_turn observation methods; his535
retention evaluation is disjoint. Einstein compaction policy/native producers and
Sch package publication remain protected. No new native build or provider probe.

Confirmed source relation: SelectedAttempt.observe_event awaits DurableTurn.dispatch,
but its acknowledgement/context/native-phase handlers perform synchronous SQLite
reads/writes on the native event loop. SelectedParticipant registry observation
already uses Coordination's joined worker. DurableTurn must use that same resource
mechanism for its declared observation family, owning a fresh connection inside
the worker; its original fence remains the only mutable attempt authority.
Use existing MroDispatch handlers and context-manager lifetime; no new family,
queue, clock, phase/status mirror or cross-thread caller connection.

Finalization also currently writes Settling twice for one already-reaped completed
backend. Existing AttemptStore.advance accepts the phase and both final facts in
one fenced transaction; preserve progress, current pointer, declared lifecycle,
UNKNOWN and failure settlement. Publication/read fences retain their original owners.

The385 finish-to-publication intervals are not per-function spans. This source
closure does not establish their attribution or claim public reply latency fixed.
Pattern BOUND-2/IMPL-13: complete the existing resource seam, not a competing reader.
AST owner/consumer closure precedes edits; focused resource/finality checks come last.
