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
#515. Schrodinger then normally joined #515 and #518 in candidate `65a28ad2`
before its first checks. One installed wheel served both owners' journeys.
Its 339 Git source files and 341 wheel files are byte-equal, with ordinary
dependency resolution, SDK 0.12.1 and existing native5184 full trust. There is no
source overlay or separate #515 environment.

The [installed callback/CLI journey](../../evidence/context-manifest-resource-owner-20261002/installed-callback-cli-receipt.json)
passed on that same prefix. Original native observations came from preserved
`b509c01`; the isolated recording fixture uses real `CommsAgent` components,
`TurnProgress`, `SelectedParticipant.select`, registry leases, coordination rows
and the canonical `WireLog` lock. Both callbacks stayed responsive while the
writer was held. The cancelled ordinary callback joined publication before
returning cancellation. Each callback produced exactly one silent manifest;
metadata and the ordinary lease identity matched their original values.

The actual installed `agent-comms context Alice --turn 1` returned its original
manifest with `text_recorded: false`. Message sequence and history stayed
unchanged. The original source registry and bus hashes stayed unchanged. Zero
provider calls, native inputs or public mutations. These are actual installed
recording-owner/resource/CLI results, not a new provider/native producer claim.

The initial pytest command refused the repository's optional xdist/coverage
arguments before collecting a fixture. That refusal is retained. Direct asyncio
execution then completed this single journey, using installed application
source and existing test support only. No failed native input was retried.
