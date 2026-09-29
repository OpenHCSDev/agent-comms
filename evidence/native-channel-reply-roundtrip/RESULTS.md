# Actual native channel reply roundtrip regression

Production RED base: main da2b43d4. Scope tests only; parent owns production fix.
Own persistent ~/wt/comms-native-reply-roundtrip-20260928 and noneditable installation.
Actual pinned native bundle native-current-5fdef596596173bd; local-only HTTP model.

Two production owners are registered and started normally, with channel membership.
The questioner attaches via actual CommsClient/socket, then publishes an ordinary
channel question naming answerer. Answerer executes actual native FULL and publishes
ROUNDTRIP_NATIVE_REPLY. Questioner must automatically receive it in actual native
bounded triage; provider chooses IGNORE, preventing recursive reply work. No manual
inbox/drain, participant insertion, claim creation, user prompt or native event stub.

Assertions preserve two distinct durable native inputs/sessions, source reply content,
ACP proven cursor feedback, two total provider requests, no third channel message,
no duplicate/replay/ping-pong beyond watcher fallback interval, owner cleanup.

RED receipt: B actual native request and actual reply reached; sender observation
must fail on current production. See red.log for executed result (not a green claim).
The post-fix receipt must exercise remaining assertions; these are not proved by RED.

Latest 22:16 skills applied: AGENT-8 production paths, AGENT-3 no duplicated legacy
fixture matrix, BOUND-1 shared typed ACP decoder, TIME-9 no adapters/aliases. New test
has zero BooleanChainTerms (no >=4-term BooleanOp). No production/live changes.

Response-awareness followthrough: after AUTOMATIC native receipt (never before it),
the same test also requires the reply ID in the existing canonical addressed source
pointer projection. This protects the separate natural-turn awareness omission
without mistaking pointers for model delivery or restoring the advisory ledger.
This added assertion remains unexecuted until Boyle supplies his production candidate.

## Candidate actual acceptance

Boyle331 candidate35d841623398ed46ada3f69ac5bb6b904324b002 merged into own
2b9df060 and noneditable-installed. green.log:1passed14.37s. Two actual production
owners/native sessions, B FULL reply, automatic A native triage containing reply,
canonical response source pointer, attached ACP VerifiedCursorObservation with
exact reply injected/covered sequence and actual durable input identity; exactly
two provider calls and two channel rows, no mandatory ACK reply/recursive wake/replay.
No user prompt/manual inbox/drain/participant or claim insertion. Cleanup passed.

candidate.log and candidate-feedback.log are retained failing test assumptions,
NOT production missing-feedback evidence: ignored triage emits selected_status=None
but actual verified cursor includes exact injected reply and native input ID.
The final assertion uses that existing typed owner; no fabricated status or weakened
receipt/content/no-loop requirement. Original red.log remains unchanged.

Optional S15 advice was relayed to Boyle331: Ambient must never wake, distinct
from automatic reply delivery/DependencyWake, reuse shared delivery and measurement
owners. S15 itself is absent in available root plan files, original and round2
archives and named worktree files; requested authoritative path from Boyle. This
receipt does not certify S15 family implementation. Current331 excludes responder
from wake recipients; mentions/ambient expansion needs explicit chosen contract,
not restoration of deleted advisory state. No duplicate production implementation.
