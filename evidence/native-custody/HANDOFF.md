# S2 native launch, attestation and retained-child custody — draft checkpoint

Depends on PR308/f757627a. Parent owns review/install. Boyle runtime dispatch,
bootstrap and worker supervision are untouched; coordination is recorded on308.

PersistentPiSession owns the actual child/channel/stderr task throughout launch,
use and retention; TurnSession no longer carries copies of those resources.
It owns reuse/auth/revision/strict-reopen decisions and retained metadata handoff.
NativeAttestation owns the correlated GetState request and actual capability
response plus expected NativeSessionIdentity. Deleted the mutable capability
flag, TurnSession.attest_input/validate_reopen/spawn_child/stderr_tail and the
resource-copy retention assignments. All direct Pi/output/watchdog/input/stats/
preparation callers use the same resource owner. Existing native identity,
input authority and revision fences remain in force. No new runtime flags,
aliases, process broker or registry. Failed/cancelled turns close the owner in
finally; retained turns preserve the same actual child.

Implementation checkpoint only: recorded native-protocol regression currently
running; actual native custody/reopen/cancellation tests are next. No readiness
claim until those cases finish. No live install or repeated unchanged308 tests.
