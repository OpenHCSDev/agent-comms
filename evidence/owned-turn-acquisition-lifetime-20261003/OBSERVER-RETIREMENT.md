# Joined cancellation and input producer retirement

Source correction after a414, not an installed acceptance receipt.

The original controls10 task observation captured shutdown awaiting InputDrain.close,
with the original observer already cancelling yet back in WireWatch.observations.
The same original private activity recorded IdentityConflict (missing participant
aggregate). Three earlier failure cases completed; the fourth teardown did not.
Executor workers were idle. This is not an attributed SQLite lock failure.

The shared join_retirement catches caller cancellation while waiting for its owned
future. Previously a later worker exception escaped from shield/result before the
saved cancellation was propagated. InputDrain.observe could catch that exception,
publish a drain diagnostic and resume its watch after its sole cancellation.
The existing join now preserves caller cancellation after successful or failed
retirement; a failed operation remains the explicit exception cause. Without
cancellation its original exception/result still propagates unchanged. All users
share this one behavior: Coordination.run_worker, ChildProcess group retirement
and stop, and BoundedRun's joined pipe exchange. No retry or guard is introduced.

InputDrain.close owns its observer and wake resources together. It closes scheduling,
cancels and joins the original tasks, then clears only their process-local resources.
The separate stop_wakes path and late observer retirement are deleted across every
caller. CommsAgent.shutdown uses its existing AsyncExitStack machinery to retire
inputs before proxies/turns and to finish registered cleanup even if one callback
fails or is cancelled. Original input dispositions, pending turns, native receipts,
registry CAS and UNKNOWN are untouched. Patterns IMPL-12/13: one shared resource
lifetime replaces the split shutdown and cancelled-worker error paths.

The NRA parser before/after covers all311 production modules. Its consumer output
includes task membership and shared joins; MRO and callback resolution were read
from source, not claimed as AST behavioral proof. Tests' original wake shutdown and
future-input refusal now invoke close. The existing actual reservation cancellation
control also releases a failing original worker after cancellation and checks the
original NotSent rollback/idle lease plus preserved worker error cause.

Original controls04/05/07/09/10 negatives remain unchanged. Controls09 failed only
command collection (unavailable xdist defaults), not execution. Controls06/08 passed
with observation wrappers and did not establish deterministic closure. Controls10
was retired through exact ProcessIdentity/Platform.send SIGINT; actual exit is
recorded. No native/provider/public process was involved. Final installed batch and
one distinct configured saved fork remain required for this expanded source.
