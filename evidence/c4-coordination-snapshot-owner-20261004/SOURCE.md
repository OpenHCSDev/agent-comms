# C4 coordination snapshot owner

Base: current main c159f39e. Parent owns history_views.py and presentation.py; Mendel owns the separate InputDrain closure.

The original C4 class-span gate has regressed: HistoryViews is 515 lines and InputDrain is 571. WireLog remains the original explicit exception. Historical installed C4 acceptance is preserved, not reused to claim current-source completion.

HistoryViews.coordination_snapshot and viewer_snapshot each assemble the same ChannelView/ThreadView roster cohort. CoordinationSnapshot will own that assembly through the original roster owners, using the caller's acquired registry, catalog, activity, pending counts and display metrics. Actor unread and human display unread remain distinct. Viewer native-file unread reads remain outside the display/bus snapshot locks. No field, schema, store, cache, type or codec is added.

MessageNotification already owns the bounded notification window. Its original reference reader will own chunking and delivery projection; HistoryViews will delegate. Original audience, assignment and read-ledger outcomes stay unchanged.

Pattern IMPL-12: duplicated cohort procedure. AGENT-6: reject relocating arbitrary methods or carving mixins to meet the line limit. A roster-cohort change should require one assembly edit, rather than independent actor/viewer assembly edits. Notification window policy should require one owner edit, rather than an external batching implementation.

Before/after evidence uses the original published NRA Package/FunctionFacts/GodClass collector over all production, tests and tools. Lexical calls do not prove dynamic invocation. Tests and the affected installed consumer path follow coherent source closure. This draft is source work, not installed or public acceptance.
