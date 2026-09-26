# Selected native prompt send admission (provider-free pilot)

## Rejected boundary

Independent review found that `96b71e0` (retained at `3a22119`) held synchronous
wire/bus/registry/SQL locks over `await stdin.drain()`. A synchronous ordinary
`registry.unregister` callback on the same loop then blocked that loop waiting
for the registry lock. Drain, cancellation and the loop deadline could not
progress. Passing subprocess exclusion tests did not address this deadlock.
Releasing the locks before drain is not a correction: buffered prompt bytes
could still flush after the owner was revoked.

## Irreversible byte admission

`native_prompt_send.send_fenced_prompt` duplicates the actual nonblocking stdin
pipe descriptor only after capability preflight and an empty transport-buffer
check. A dedicated thread (not the shared executor queue) acquires admission and
writes the serialized prompt directly with `os.write`. The selected-prompt path
does **not** call `StreamWriter.write` or `drain` for prompt bytes.

Each successful `os.write` is an irreversible byte-admission point. The canonical
wire → bus → registry → SQL scope stays held through the final byte. The scope
rechecks the exact registry incarnation/turn, SQL generation, input reservation,
claim, full attempt and durable request binding. Its SQLite connection belongs
to the writer thread. Canonical store locks, SQLite acquisition and the binding
snapshot lock fail nonblocking on contention. Admission refuses use on any
running asyncio event loop, rather than allowing the original deadlock pattern.

Complete OS writes are **not** native acceptance, assembled-context evidence,
provider receipt, a response permit, or a proven injected cursor. Later
live-recorded native request-digest/context proof plus exact prelaunch binding
may advance a separate *current-owner* source cursor only after the sealed
selected stage settles. A missing/UNKNOWN earlier source leaves a gap; the
bounded cursor is informational, not provider acceptance, ACK, response or
write permission. Owner replacement cannot promote historical evidence into a
new current cursor. See `docs/ordinary_nk_delivery.md` for its capacity and
schema-v2 limitations.

## Bounded uncertainty and cleanup

- The writer checks a monotonic deadline independently of the owner loop, capped
  at five seconds (or the remaining native deadline, whichever is shorter).
  Nonblocking writes use bounded readiness waits; no queued prompt buffer can
  remain after the writer exits.
- A same-loop lifecycle callback may synchronously wait for admission, but the
  writer's deadline and release do not require that loop to resume. This avoids
  the circular wait; it does not promise zero UI blocking during that interval.
- Cancellation signals the writer and waits for it to stop. Repeated cancellation
  cannot abandon a writing descriptor. Child termination/reaping is separately
  shielded against repeated cancellation.
- Partial writes, deadline expiry, cancellation, broken pipes, or an error after
  any bytes were written grant no successful completion. The reserved input
  remains UNKNOWN/unreplayable. There is no automatic resend, even if the native
  process may already have received enough bytes to act.
- Capability/preflight drain also has a deadline; the deadline is no longer
  limited to reading the next event.

Bounds assume functioning local OS/filesystem primitives, not a malicious or
permanently hung kernel/filesystem. Process death remains uncertain, never
recovery/retry authority. No broader shell/child-write enforcement is implied.

## Acceptance evidence

`tests/test_native_send_admission.py` exercises real pipe capacity/backpressure,
a synchronous same-loop unregister callback, stalled send timeout, cancellation
and repeated cancellation (including during child cleanup), suspended preflight
drain, real child reap, no second launch of uncertain input, stale owners and
subprocess lock exclusion. Potentially hanging cases execute in bounded isolated
test processes. Fake native children never assert model/native acceptance.

The independent old fake-boundary reproducer now receives a typed refusal when
it tries to open admission on the loop; the real adapter tests independently
exercise the replacement path. This successor requires its own exact-byte review.
