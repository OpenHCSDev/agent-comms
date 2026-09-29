# C4 current ownership scope

Measured at current main697bba42 before production continuation.
HistoryViews is478 lines and already below the500-line plan threshold; do not
extract more components from it merely because its historical receipt said1012.
CommsAgent remains549 lines. WireLog is excluded by the original C4 decision.

The remaining state-owning responsibility is ACP native-cursor observation and
publication: scope capture, projection revision, last announced observation,
nonblocking refresh and publication to the attached runtime. Its state currently
lives in CommsAgent as two session-keyed dictionaries. The native input/cursor
proof remains owned by NativeSourceCursor/CursorOwner and the durable ledger.
A transport presentation component must consume those original proof owners,
never become another admission authority or source cache.

Before implementation, trace every effects/session/drain/compaction consumer.
Close all direct private dictionary accesses and old helper callers when moving
the responsibility. Use the existing CursorEnvelope/observation family. Do not
carve mixins, duplicate private launch pins or keep compatibility helper aliases.
State ownership and a documented reduction in edits for an additional transport
consumer must accompany class-size closure. Mere relocation is not completion.

The parent owns this draft. Arendt owns complete turn lifecycle425/211,
Schrodinger owns ThreadPresentation identity binding426/210, and existing
shared-file owners must coordinate directly before overlapping edits.
