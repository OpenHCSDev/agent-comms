# Canonical original transcript time

Base: main `01a9b7dd`. Owner: native/backend sidecar. Tesla owns Toad142 callers and installed return-paint acceptance; Carver T4 AgentProcess and Noether151 plan boundary are untouched.

## Contract and ownership

BOUND-1/BOUND-2: `NativeEntry.timestamp` remains the external native journal ISO8601 value, without changing saved files or proof readers. Its existing `events()` boundary now decodes the clock once per native record before projecting all parts. Message and compaction declarations own content through `_events()`, while their common parent guarantees every projected part retains its source time. `TranscriptEvent.timestamp: float | None` is Unix seconds including fractional seconds, transported by existing FieldCodec through ACP. Missing, malformed or timezone-less external time stays unknown; never substitute the current clock. Live receipt time is a separate UI decision.

TIME-9: no codec subclass, time wrapper, parallel timestamp store, migration or alternate runtime schema. Existing dataclass replace/StreamingMerge automatically preserves time and refuses merging different recorded times. Actual native timestamps remain exact external ISO strings; ACP uses the declared seconds projection.

## Deletion and completion

Two subclass overrides of the full projection boundary are replaced by content hooks; no consumer can omit the clock for a content subclass. Core has never fabricated a replacement timestamp; that default is in Toad, owned by Tesla. This urgent bug adds a missing fact, not a net deletion claim. Deferred selected dry-run RPC deletion has no implementation edits.

Initial boundary checks: five pass (external offset/fractional times, split/routed input, compaction, merge, and unknown time). Actual native plus ACP transport/rebuild and installed Toad return-paint are pending. Production is not installed by this branch.
