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

## Verified checkpoint

The protected operator stdout birth timestamp is08:49:51.475281Z and finalmtime
08:51:55.228487Z. R1's failed checking starts08:52:19.567983Z. Only timestamp
metadata was read here; no private preimage or credential-adjacent content is
published. The wire-held launch interval cannot directly explain this wait.

The installed Core000/nativee36 configured R1 cold get_state probe attested in
741ms, zero messages/prompts, empty stderr, child retired. The actual controlled
native regression then proved that tracked execution discarded an already-ready
get_state response after a busy Python owner resumed its deadline: baseline
failed at0.844s before prompt admission. Backend/preparation had the required
reader scheduling behavior already. This is a reproduced defect; the missing
historical native IPC prevents claiming its exact historical wait resource.

Tracked execution now acquires through original PersistentPiSession.open, uses
its PiSessionChild/PendingAttestation and consumes ProgressWatchdog.read. All
three cold callers obtain NativeStartupAdmission through TurnSession. Lease
waiting precedes readiness timing, release follows attestation, and acquired
scope callbacks settle child, pending requests and tool socket. Readiness and
model budgets are unchanged. No whole-turn timeout has been added.

Deleted authorities: tracked proc/reader/stdin aliases, initial_session_id,
prompt_response, its capability classifier, duplicate child/stderr startup and
timeout reader; backend prompt_dispatched. PromptAdmission owns unwritten versus
writer-entered state. The original correlated acknowledgment owns its successor.
NativePiInputNotSent carries actual attestation/control evidence before the
prompt writer is entered. After writer entry, failed/cancelled/EOF input remains
uncertain and unreplayed. NativeSessionPreparation remains unwritten on success.

One PiSessionChild failure scope retains bounded stderr for native and unexpected
producer failures, preserving original exception cause/notes. Existing private
diagnostics publishes nominal no-prompt evidence and stderr; SelectedRequest
uses the exception's declaration-owned public description. Native reservations
and failed goals remain protected; no absence-based recovery is introduced.

Private installed evidence: seven actual startup/EOF checks pass23.50s; one
bounded-triage→FULL journey executes real read/edit/write/bash tools and publishes
one reply in5.27s. Startup-wait cancellation and admitted native cancellation
also pass; EOF diagnostic publication initially exposed a misplaced inherited
property, which was corrected and the affected installed EOF gate passed.
The95s deadline-removal and89s UI lifecycle gates are not repeated.

Final publication wheel repeats only the affected cancellation/EOF evidence
after extending retention to all original exceptions: two actual selected
native negative paths plus the authority seal pass in7.14s.

Production diff:168 lines deleted,233 added, including acquired-scope indentation
and import formatting. The increase supplies the previously missing unwritten
admission member and retained original diagnostic evidence for every cold caller.
Same-run official census across the eleven changed files: dispatch subject/arm
counts unchanged, long boolean terms−4, foreign absence probes−2, string-key
subscripts net0. The extra private diagnostic field is serialization at the
existing owned boundary; no protocol value is decoded again. This is a bounded
surface check, not a global audit or full-suite claim.

The committed sanitized receipt is
`evidence/canonical-native-initialization/receipt.json`. Private original traces,
native fixture journals and stage provenance remain in the owned fixture root.
Source/installed-native acceptance does not claim a new live default or a retry
of the failed R1 input. CI remains deferred.

## Format and remaining integration scope

This native checkpoint adds private diagnostic evidence only; no public phase
ABI, native format, durable input schema or proof is reset. It is based on current
main095fc9c9, which includes C3 Thread-member retirement. A public installation
from that main therefore still depends on parent-owned approval of442's
original typed admission/all-stop/both-carrier retirement/guarded route
publication/target launch. That separate phased lifecycle remains Arendt's
receiving scope. No new route publication or restart is performed by443.
