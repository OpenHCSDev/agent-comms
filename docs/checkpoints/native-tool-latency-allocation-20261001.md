# Native tool and whole-turn latency follow-up

Integration owner: Arendt. Isolated WT comms-native-tool-latency-allocation-20261001.
Base3d537977. Draft-before-implementation checkpoint. Parent sole live executor;
frozen483 is unchanged and independent.

Ordinary tools and whole turns remain slow. Merged454 proved a nonreading socket
observer stall and bounded its transport resource; it did not prove the cause of
historical129s waits or finish general latency. Retained-context proof acquisition
was separately reduced from~2s to~40ms through the original acquired resource.

Allocate latest original comms428/NRA traces through native dispatch, response
headers, first event/delta, tool start/end, awaited callbacks, journal append and
ACP publication. Join only original request/input/turn/incarnation witnesses and
measured clock brackets; journal creation is not tool start. Reuse original
RequestProgress/NativePhase/ProfileTrace owners, not a new performance store,
semantic mirror, deadline, or retry mechanism. No provider-capacity attribution
without transport evidence and no alternate provider. No new configured fork
call until existing saved traces have been allocated; at most one bounded call
is authorized if a concrete remaining boundary requires it.

Kepler owns NEW484 inner binding-lock/admission contention (d0c838 seq273).
Coordinate that seam directly; do not edit his source or duplicate the lock fix.
Parent owns483 public execution. Source-global broker/sidecar/transport lifecycle
closure is this follow-up's scope, with representative saved native history.
Original public/UNKNOWN attempts, native/proof bytes and blocked goals remain
unchanged and never replayed.

Installed source finding: the managed project handoff extension launches a new
CLI process before every ordinary tool. Three actual read-only calls measured
539–553ms. This callback is outside the model-request observation. The original
project/owner relation must use the existing owner transport, not cached state.
See evidence/native-tool-latency-allocation-20261001.

Next: close that authority relation and instrument the existing real retained
native read fixture at preparation/admission/execution boundaries. The 121s
model request and 129s historical journal gap remain separately scoped; there
is no provider-capacity attribution or claim that general latency is fixed.
