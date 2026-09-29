# Queued cross-runtime owner handoff

Owner: Mendel. C2 Core419 followthrough; separate from full C3 Core421 and
selected-summary fixture Core422. Base main ab3397a6.

Concrete blocker: an active old runtime-journaled-original owner cannot attach
from the reviewed summary-accounting runtime because receipt_frontier is required.
The current queue wrongly compares source interpreter with target watcher.

Extend the EXISTING queue and lifecycle authority. Keep exact OwnerRestartSelection
process birth, owner/admission generation and source owner environment. Capture
reviewed target interpreter and native launch authority separately. Preserve root,
auth/model/history/source arguments while replacing only target runtime settings.
Wait for idle via existing inotify watcher. No interruption or UNKNOWN replay.
Persist attempt before retirement; ambiguous retirement/launch remains terminal
uncertain. No second watcher mechanism, PID registry or legacy decoder.

Acceptance: bounded serial continuous private native/ACP journey starts owner
under a distinct source interpreter, blocks provider response, queues from target,
proves no early retirement, releases response, observes exact replacement, then
attaches and sends distinct new native input with retained history. Existing
cancel/stale/uncertain controls must survive. Verify source auth/model settings,
target launch pins and owned child teardown; credentials never in queue receipt.
Use installed Python/dependencies with explicit source candidate provenance.
No global package/runtime change, paid call, live root mutation or activation.
Coordinate parent before actual queue activation; parent's fenced idle handoff
can continue independently.

Scratch owner/purpose:
/home/ts/.cache/agent-scratch/comms-queued-cross-runtime-handoff-20260929
stores bounded acceptance logs and private fixture receipts.
