# Context observation publication resource

Singer owns Core #515. The existing `NativeContextManifestData.record` owns
publication of the original SDK observation and now schedules that complete
operation through the existing `Coordination.run_worker`. Both original event
families await the same method. The selected forwarding method is deleted.

The ordinary callback previously acquired the canonical writer synchronously on
the event loop. The selected callback had a separate worker wrapper. Moving the
resource lifetime to their existing shared declaration closes both consumers.
`WireLog.record_context` remains the sole sealed writer; its lock order and
append are unchanged. Existing `join_retirement` joins an acquired operation
before propagating cancellation, so caller custody cannot retire ahead of a
late write.

## Source and callers

Normal merge `39705fd8` includes main `b1e235e1`, including #489 and #506.
The complete production delta is three files, eight added and seven deleted
lines. The lease, selected native-source and compaction classes remain intact.
Arendt granted the declaration and ordinary callback; Kepler granted the
selected callback and forwarding deletion. #516's configuration files are
disjoint.

- `TurnContextObserved.context` and `ContextObserved.context` carry the same
  `NativeContextManifestData` declaration.
- `SelectedParticipant.observe_context` retains `require_active_turn` and
  `require_turn_lease`, then awaits that declaration.
- `TurnProgress.observe_context` awaits it with its original constructor lease.
- `for_turn` remains pure and synchronous. Original identity, segment metadata,
  counter and wire publication semantics do not change.

[Before](../../evidence/context-manifest-resource-owner-20261002/owner-before.json)
and [after](../../evidence/context-manifest-resource-owner-20261002/owner-after.json)
map the declaration, consumers and bound worker operation using existing NRA
AST owners. NRA's Python 3.11 parser omits `tracked_turn.py`; candidate-version
Python 3.14 standard AST parses that file. This is not a configured NRA 3.14
environment. No Python parse omission remains. Dynamic receiver resolution,
reflection and unchanged native JavaScript remain explicit semantic boundaries.

## Receiving boundary

#516's configured saved-fork journey had already completed before this source
was integrated. Its frozen prefix and proof remain unchanged and do not qualify
#515. The remaining check belongs in the next matching receiving environment:
the installed original callbacks, canonical writer and manifest CLI. It detects
event-loop blocking or an omitted await, duplicate/missing publication, and
resource retirement before publication finishes. No new provider/native input
or repeated #516 gate is needed for this resource change.
