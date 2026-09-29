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

## Verified checkpoint

Actual serial installed-source CLI / native / ACP acceptance:5 passed54.37s.
Source runtime-journaled-original-20260929 with native776; target Core candidate
from mainab3397 plus this patch via absolute PYTHONPATH, reusing the installed
Core412 Python/dependencies with native7817. No new target Core wheel installed.
Source starts through its installed agent-comms CLI comms_start declaration.
Target watcher receives the target's explicit retained root/package authority
even when default-route settings were absent from its parent environment.

The same continuous fixture proves: busy input settles before retirement; exact
source process dies; replacement uses target interpreter/native launcher/package
and source auth/config/model/thinking; retained native history prefix survives;
fresh target ACP attach and new input produce three distinct native input IDs,
three loopback provider requests, with no input replay. Then cancellation, stale
selection and ambiguous post-retirement launch remain terminal without retry.
All fixture owner identities and watcher children are dead after teardown.

Evidence: actual-handoff-second.log and its private wire/session/queue receipts.
First setup failure preserved in actual-handoff-first.log: new Core correctly
refused old native manifest, and a relative PYTHONPATH selected old installed
code in a child cwd. Driver uses the true source CLI and an absolute target
source path; no attestation or protocol bypass was introduced.

Internal queue schema now records source interpreter separately from target
launch policy. Existing old-format queue receipts need an explicit cutover
disposition before this runtime reads that directory; no legacy reader is added,
and no live receipts or uncertain attempts were changed. Parent/user already
restarted pr159 manually. No live queue activation is needed or performed.
