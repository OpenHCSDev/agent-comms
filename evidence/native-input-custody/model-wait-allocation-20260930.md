# Original long model request allocation

Arendt owns native/model/tool latency. This is read-only original evidence, not a
new provider call, replay, installed acceptance claim or provider-capacity diagnosis.
The [metadata receipt](model-wait-allocation-20260930.json) records original request
1765eb2b, native process identity, turn/admission identities and journal parent chain.
It contains no original message bodies, arguments, credentials or reasoning text.

## Measured request

The same native request clock allocates 120.935 seconds as follows:

| Boundary | Duration |
| --- | ---: |
| Request preparation through dispatch | 58.983 ms |
| Dispatch to first WebSocket event | 288.455 ms |
| First WebSocket event to first consumed delta | 36.075 s |
| First consumed delta to stream close | 84.504 s |
| Stream close to request cleanup | 8.197 ms |

The connection acquisition itself took 5.712 ms within preparation/dispatch.
The first transport event was observed at 347.437 ms. First delta means any
thinking, text or tool-call delta; the first visible text clock is not retained.
The committed assistant contains thinking and one write tool call, with original
`toolUse` stop reason. Stream close/finished observations themselves are cleanup,
not a successful turn settlement witness.

Native awaited callbacks total 145.753 ms across 1,427 calls; longest 7.912 ms.
Their owner wraps before-provider payload handling and awaited native
message-start/update/end publication, including extension handling and original
journal append. Tool execution and preparation for the next assistant request
are outside that timing resource.

The independent Python publication owner records 28 completed spans totalling
15.847 ms between first and last progress receipts. This measures both
`TurnProgress.transition` phase notifications and `TurnProgress.consume` event
emission through `effects._emit_event`. It does not measure all durable routing,
registry work or UI rendering. Its cumulative recorded 243.840 ms maximum occurred
before this request's first Python receipt and cannot be attributed to this request.
Native and Python durations are separate observations and are not added together
or subtracted across process clock origins.

Native usage input 7,434/cacheRead 158,336/output 1,950/reasoning 516 shows a 95.515%
prompt cache fraction. Output already includes reasoning. Neither cache fraction
nor reasoning token count provides a timing allocation. No retry or second
transport dispatch is observed in this request; provider-side queue/compute
allocation is absent from these diagnostics.

## Tool result and cancellation seams

The same native process/session journal parent chain proves three read results,
then this long request, then a successful write result and a new model request.
Native wall-clock metadata records 33 ms from the last read result journal row
through this request's preparation, and 3 ms from the write result row through the
next request's preparation. The assistant journal row through write result row is
774 ms. These are aggregate seams with millisecond precision, not pure file IO
or native tool execution durations. Original ToolExecutionStart/End producer
clocks were not retained for these historical tools.

Native 593b `pi-agent-core/dist/agent-loop.js` awaits tool result publication,
then `turn_end`. An aborted signal emits `agent_end` and returns before consuming
queued steering/follow-up inputs. For a continuing tool turn, native
`prepareNextTurn` (including compaction/extension refresh) precedes creation of
the next request observation. `AgentSession.abort()` cancels retry/compaction,
then aborts the agent and awaits original idle. This historical request has
original `toolUse` and subsequent successful tool/next-request evidence; it is
not a cancellation or an UNKNOWN retry witness.

## Maintenance relation

The separate accepted retained-native tool gate fixes the concrete repeated
44.7 MB context-proof decode at `TrackedTurnSession.tool_started`: approximately
2 seconds per proof read becomes 38–43 ms per acquired observation, with original
prefix/private-path/current SQLite proof checks retained. That measured local fix
does not explain the 36-second pre-delta or 84-second stream interval above.
Remaining transport/model allocation requires original event timing evidence;
this receipt does not rename an unknown interval as provider capacity or IO.
No extra timing store, semantic mirror, deadline or retry policy is added.
