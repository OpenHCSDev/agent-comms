## Implemented

InputDrain owns ACP original/followup acceptance, durable input dispositions, queued/restored IDs, native-start/refusal handling, inbox cursors, wake/drain tasks and queue teardown. ACP keeps protocol routing, turn orchestration and compatibility accessors; its internal queue consumers now use the component. No registry/generation policy changes.

Fixes the actual PR95 queued-during-summary failure. Compaction permits an exact unattempted future input only while the current process/owner/turn still holds its durable queue receipt. Original, steer, clear/promote, changed/deleted/bound queue, old UNKNOWN and owner changes remain fenced. Source comparison now includes only owner-relevant inputs and existing DeliveryScope bus messages, so foreign ingress does not cancel a summary. Rechecks cover commit, terminal ACK and original bind. No replay from durable UNKNOWN, extra queue store, native bundle change or live deployment.

## Local verification

- 150 core ACP/input/selected-admission tests pass.
- 39 real native commit and queue tests pass using the existing pinned package read-only and synthetic provider streams; includes actual selected summary -> native commit -> strict reopen -> original and queued followup each once in order, with and without foreign ingress.
- 53 final candidate queue/channel/private delivery cases pass after PR152 integration.
- Full-context NRA scan: 79 detectors, none omitted, no findings in selected files. Authored component ownership is verified by tests, not claimed as native equivalence proof.

Parent owns fresh configured-provider acceptance and serial integration/deployment. No CI wait. Evidence and final handoff are being completed on this branch.
