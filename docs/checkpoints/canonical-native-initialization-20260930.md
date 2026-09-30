# Canonical native initialization

Owner: Arendt, S14. Receiving scope: original native child acquisition,
NativeStartupAdmission, attestation, command admission and private diagnostic
publication across TrackedTurnSession, backend and preparation. Parent retains
all public installation, route publication and owner restart authority.

Original incident evidence is retained in PR442 at8b5e677f, in
`docs/checkpoints/native-preflight-incident-20260930.md`. The original refactor-r1
seq160 reservation is preserved UNKNOWN. No input is replayed, drain resumed,
goal retried or live process signalled by this work.

## Required relation

One acquired native child owns its initialization lease and attestation. The
same relation applies to tracked execution, ordinary backend and saved-context
preparation. Admission distinguishes a command not yet written from a written
command whose outcome is uncertain. A failure before prompt admission must carry
the original control-command witness, not a parsed phase string or an inference
from absent journal fields. Private diagnostics retain the child's bounded
stderr and transport evidence through the existing diagnostic owner.

Delete tracked duplicate child startup/attestation and consumer reconstruction
in place. Do not increase readiness bounds, add whole-turn deadlines or create a
parallel lifecycle store. Patterns: IMPL-12/13, IDEN-3 and IMPL-10.

## Acceptance and limits

First establish actual cold native IPC with a private zero-prompt R1 setup:
openrouter/z-ai/glm-5.3-flash, thinking off, installed nativee36. Use existing
Pi launch, child custody and attestation declarations. Reuse actual native
startup and controlled-provider journeys for contention, ready-pipe scheduling,
genuine unresponsive child refusal and prompt/noReplay settlement.

Historical native PID/stdout/stderr were not retained. The original launch batch
returned24.34s before the failed attestation began; tracked TRIAGE disables
extensions. Neither batch-lock contention nor an extension deadlock is proven.
Keep these limits separate from any reproducible current mechanism defect.

## Resources

Reuse the installed runtime; no environment build or extra fleet. The resource
guard reports critical16.4GiB swap with18.2GiB available RAM. Limit the initial
probe to one child, one correlated read, no provider prompt and a bounded outer
process lifetime. Private evidence lives under the owned persistent
`.native-init-fixtures/` directory, with700 directories and600 records. Preserve
failure traces; retire all owned child processes after each run.
