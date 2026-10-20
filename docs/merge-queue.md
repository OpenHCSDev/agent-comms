# Automatic merge queue

Owner: pi-cn-load, P269, draft PR #2023. Not enabled or accepted.
The takeover preserved pi-install-gate's newer published 307e5955e, a descendant
of the ordered f0dc3df0c checkpoint, and merged current main without rewriting
history. First takeover push: fb6074f3e. P714/#1947 is merged as the review-owner
dependency, not copied into another verdict implementation.

GitHub owns queue order, proposed merge identity and failed-entry removal.
QueueCheck and RunEvent members own host execution and admission. The existing
workflow runs debt-src, debt-tools and install-rehearsal on merge_group.
No commander's merge scheduler is involved after bootstrap.

## Review and installer ownership

ReviewedHeadVerdict.context is the sole review-context declaration. Its existing
publisher and the queue reader both use it. The status event passes its context
as data; the host compares it with that declaration. No repository variable
mirrors the context. No native GitHub approval is required: the contributors all
use the same GitHub identity and cannot approve their own PRs.

The queue invokes PracticeGateCommand. PracticeCopy uses the existing CheckRoot
owner to retain the bus tail, bounded saved-session tails and their production
indexes; it does not snapshot the whole desktop bus. PracticeCopy owns the 3 GiB
cap and checks measured bytes against current free space. It never admits an
empty substitute or a skipped installation. The gate checks out the proposed
revision, then rehearse invokes target/tools/upgrade_bus.py, including --help and
the actual rehearsal. It does not invoke the live checkout's installer. GateResult/MachineResult carry
typed failure reasons back to the queue. CheckFailed carries the actual completed command and delegates its public
summary to the QueueCheck member. Ratchet summaries decode introduced_lines
and print Measure file:line. Installer summaries return the declared
UpgradeRefused message through the existing typed gate result. Raw child
stdout/stderr, successful rehearsal steps and bus data remain private.

## Isolation and lifetime

The existing runner ts-gaming-agent-comms-merge (ID 21) is registered but offline.
Labels: self-hosted, Linux, X64, ts-gaming, agent-comms-merge. Its bubblewrap
namespace exposes vendor runtime, work/log directories, system binaries and a
sealed runner home, not host homes, buses, personal tokens or desktop sockets.
The SSH key admits only the fixed host entrypoint. GitHub run identity and the
workflow path are verified before checks. No generic hosted runner reads a bus.

The host derives home/user from its environment and OS identity. There is no audit path on the wrapper's PYTHONPATH: debt_ratchet derives it
from the repository submodule itself. Host.fetch initializes/updates the pinned
submodules. No unit checkout is a dependency of production gates.
There is no shared host wrapper timeout. Ratchet members own 600 seconds,
install owns 3000, and enqueue/dispatch share their command capability's 120.
YAML ceilings are 15, 60 and 5 minutes respectively. Local development ratchets
retain the ordered timeout 300 and faulthandler.

Only explicit gate failure reasons and source annotations cross the runner
boundary. Full child diagnostics, copied state and successful rehearsal results
remain on the host. A storage or allocation refusal fails the gate; it never
turns into a skipped or successful practice install.

## Settings Tristan must change (no settings changed by this worker)

- Settings → Rules → Rulesets → Automatic merge queue (25439636): change Enforcement from Disabled to Active for main, after reviewed bootstrap code is on main.
- In that ruleset → Merge queue: Merge method Merge; maximum group/build/merge size 1; require debt-src, debt-tools and install-rehearsal from GitHub Actions.
- In that ruleset → Require a pull request before merging: set required approvals to 0; the staged rule currently incorrectly requires 1 native approval.
- Settings → Actions → Runners: reuse registered runner ts-gaming-agent-comms-merge (ID 21), with the labels above; do not register a duplicate. If it must be replaced, use New self-hosted runner → Linux → x64 with those same custom labels.

Runner registration does not start its host process. The previous owner confirms
that its restricted key was never added to authorized_keys and no runner
process/service has started. An authorized operator must install the reviewed
fixed-command, loopback-only key restriction and start the reviewed isolated
launcher from main, retaining the existing private runner credentials. Do not
launch the previous owner's queue-qualified/ scripts; those are old qualification
evidence, not installed entrypoints. No runner launch, authorized_keys edit,
queue enablement, native approval, production install or restart was performed
by this worker.

## Practice proof and later activation

The latest owner ruling replaces real-queue proof with a practice installation
on the bounded retained-tail copy. Report that command's exit code and the pushed
revision; unit checks alone are not the practice-install proof. Both source and
tools ratchets must exit zero. SourceLines reports actual growth without a
budget or blocking the merge; structural findings still block through their
owning Measure implementations.

The disabled queue, offline runner and unauthorized key are not prerequisites
for this proof. They remain untouched; later production activation requires the
operator actions above. No deliberate failing GitHub branch is claimed here.
The copy does not need room for a 26 GiB desktop snapshot. Its admission uses
actual copied bytes and actual remaining space, not a second queue policy.

Worker checkouts and receipts stay under /home/ts/wt/pi-cn-load--P269 and
/home/ts/.cache/agent-scratch/pi-cn-load, below the combined 3 GB limit. The old
runner state remains with its existing owner; its credentials have not been
copied or deleted.
