# Idle drain source acquisition

Original556/351 default UI reports fresh UnavailableDrainDiagnostic/Errno11 for
owner generations2207..2225, without a new tracked input or active turn.
Original activity rows are the evidence; old per-turn diagnostics are not these
failures. Publicroot and originals remain untouched.

The new554 opportunistic revision read calls WireLog.certified_read(blocking=False).
Its ordinary exclusive bus contention escapes to InputDrain.observe's broad
error-health path. The observation is not delivery admission or a missing inbox.
Move this observation onto the existing async physical-lock acquisition resource,
release its source certificate before opening the original SQL worker, and retain
the exact before/after observation comparison. The existing coordination reader's
bounded acquisition policy applies; no new timeout/cap or retry mechanism.

WireLog owns the shared certificate/marker/currentness lifetime for sync and async
consumers. No shared-lock weakening, generic error suppression, input replay,
new phase/flag/cache/store, or native/prompt-admission change.

Claims InputDrain/WireLog and affected original idle observation tests. Arendt
notified directly; Sch owns disjoint FD custody read and independent555 artifact.
Existing555 source38affa is frozen in its preserved branch. Same checkout reused.
Source semantics and existing NRA AST first; one affected resource/drain control
batch and installed read-only saved-history path last, no provider/public input.
