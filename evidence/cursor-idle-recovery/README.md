# Idle cursor recovery — PR299

## Observed live, read only

Runtime `runtime-cursor-recovery-20260928`, base989189d0. Subscribed directly to
existing owner Unix sockets using the canonical subscribe request; no input,
restart, native session write, proof mutation, or provider call.

`live-before.json`: nra-architecture callback21=none; trusted ready22=unavailable,
same valid CursorScope; no later callback during an18-second observation window.
PR95 returned none on its trusted snapshot. ScopeNone was not observed.

## Cause and correction

PR295 invalidated the announcement cache on trusted load, but the source-revision
idle shortcut skipped `_drain_private_nk`, including cursor publication. A file
lock can be released without changing any watched source revision.

The existing idle branch now asks the existing ACP projection owner to refresh
an unresolved observation. CursorObservation owns whether another read is needed;
the announcement cache stores CursorEnvelope instead of a JSON signature. A
resolved unchanged observation keeps the fast path. A deferred contended read
invalidates publication caching, without broadcasting fake unavailability or
changing the client's proof. Unavailable observations remain retryable.

No changed authority, wire schema, stored schema, native package, no replay, or
input-admission changes. A genuinely missing owner scope still requires trusted
binding; callbacks remain unable to establish authority. Blank-history paint is
Carver's separate assignment.

## Verification

- `red-idle-loop.log`: baseline989189d0, genuine kernel bus lock, owner identity,
  coordination store, socket delivery, production periodic drain: timeout,
  1failed3.48s. Earlier `red-native-loop.log` exposed a test receipt ordering bug;
  the final test ignores callbacks older than the trusted snapshot.
- `green-initial.log`: corrected source recovery plus six existing idle checks,
  7passed3.26s.
- `installed.log`: separately built wheel imported via its installation path;
  real lock/socket/periodic recovery, six idle tests, six subscription rejection
  tests, 13passed3.55s. The recovery case also proves20 unchanged idle drains
  allocate no additional cursor revisions, and no history/input was created.
- `owner-handshake*.log`: failed harness attempts retained. First omitted enabling
  the runtime socket; second held the bus lock across the entire subscribe
  handshake, blocking transcript replay before ready. Neither is a green proof.
  Final regression contends only the trusted metadata read, matching the observed
  live failure, then exercises the production periodic path and socket delivery.
- `red.log`: initial pytest launch lacked an addopts override; no test ran.

This proves the installed owner read/projection correction, not activation in
existing live owner processes. Parent owns integration/deployment. No CI gate.

Production delta:28added8deleted; test delta:47added23deleted. Additional lines
encode retry behavior on the existing observation family and exercise the real
idle scheduling boundary; no parallel retry service or cache authority.
